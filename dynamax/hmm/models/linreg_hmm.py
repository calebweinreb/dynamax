import jax.numpy as jnp
import jax.random as jr
from jax import vmap
from dynamax.hmm.models.base import ExponentialFamilyHMM
from dynamax.parameters import ParameterProperties
from dynamax.utils import PSDToRealBijector
from tensorflow_probability.substrates import jax as tfp

tfd = tfp.distributions
tfb = tfp.bijectors


class LinearRegressionHMM(ExponentialFamilyHMM):

    def __init__(self,
                 num_states,
                 covariate_dim,
                 emission_dim,
                 initial_probs_concentration=1.1,
                 transition_matrix_concentration=1.1
                 ):
        """_summary_

        Args:
            initial_probabilities (_type_): _description_
            transition_matrix (_type_): _description_
            emission_matrices (_type_): _description_
            emission_biases (_type_): _description_
            emission_covariance_matrices (_type_): _description_
        """
        super().__init__(num_states,
                         initial_probs_concentration=initial_probs_concentration,
                         transition_matrix_concentration=transition_matrix_concentration)
        self.covariate_dim = covariate_dim
        self.emission_dim = emission_dim

    @property
    def emission_shape(self):
        return (self.emission_dim,)

    @property
    def covariates_shape(self):
        return (self.covariate_dim,)

    def initialize(self,
                   key=jr.PRNGKey(0),
                   method="prior",
                   initial_probs=None,
                   transition_matrix=None,
                   emission_weights=None,
                   emission_biases=None,
                   emission_covariances=None,
                   emissions=None):
        """Initialize the model parameters and their corresponding properties.

        You can either specify parameters manually via the keyword arguments, or you can have
        them set automatically. If any parameters are not specified, you must supply a PRNGKey.
        Parameters will then be sampled from the prior (if `method==prior`).

        Note: in the future we may support more initialization schemes, like K-Means.

        Args:
            key (PRNGKey, optional): random number generator for unspecified parameters. Must not be None if there are any unspecified parameters. Defaults to jr.PRNGKey(0).
            method (str, optional): method for initializing unspecified parameters. Currently, only "prior" is allowed. Defaults to "prior".
            initial_probs (array, optional): manually specified initial state probabilities. Defaults to None.
            transition_matrix (array, optional): manually specified transition matrix. Defaults to None.
            emission_weights (array, optional): manually specified emission weights. Defaults to None.
            emission_biases (array, optional): manually specified emission biases. Defaults to None.
            emission_covariance (array, optional): manually specified emission covariance. Defaults to None.
            emissions (array, optional): emissions for initializing the parameters with kmeans. Defaults to None.

        Returns:
            params: a nested dictionary of arrays containing the model parameters.
            props: a nested dictionary of ParameterProperties to specify parameter constraints and whether or not they should be trained.
        """
        # Base class initializes the initial probs and transition matrix
        this_key, key = jr.split(key)
        params, props = super().initialize(key=this_key, method=method,
                                           initial_probs=initial_probs,
                                           transition_matrix=transition_matrix)

        if method.lower() == "kmeans":
            assert emissions is not None, "Need emissions to initialize the model with K-Means!"
            from sklearn.cluster import KMeans
            km = KMeans(self.num_states).fit(emissions.reshape(-1, self.emission_dim))

            _emission_weights = jnp.zeros((self.num_states, self.emission_dim, self.covariate_dim))
            _emission_biases = jnp.array(km.cluster_centers_)
            _emission_covs = jnp.tile(jnp.eye(self.emission_dim)[None, :, :], (self.num_states, 1, 1))

        elif method.lower() == "prior":
            # TODO: Use an MNIW prior
            key1, key2, key = jr.split(key, 3)
            _emission_weights = 0.01 * jr.normal(key1, (self.num_states, self.emission_dim, self.covariate_dim))
            _emission_biases = jr.normal(key2, (self.num_states, self.emission_dim))
            _emission_covs = jnp.tile(jnp.eye(self.emission_dim), (self.num_states, 1, 1))
        else:
            raise Exception("Invalid initialization method: {}".format(method))

        # Only use the values above if the user hasn't specified their own
        default = lambda x, x0: x if x is not None else x0
        params['emissions'] = dict(weights=default(emission_weights, _emission_weights),
                                   biases=default(emission_biases, _emission_biases),
                                   covs=default(emission_covariances, _emission_covs))
        props['emissions'] = dict(weights=ParameterProperties(),
                                  biases=ParameterProperties(),
                                  covs=ParameterProperties(constrainer=tfb.Invert(PSDToRealBijector)))
        return params, props

    def emission_distribution(self, params, state, covariates):
        prediction = params["emissions"]["weights"][state] @ covariates
        prediction +=  params["emissions"]["biases"][state]
        return tfd.MultivariateNormalFullCovariance(prediction, params["emissions"]["covs"][state])

    def log_prior(self, params):
        lp = tfd.Dirichlet(self.initial_probs_concentration).log_prob(params['initial']['probs'])
        lp += tfd.Dirichlet(self.transition_matrix_concentration).log_prob(
            params['transitions']['transition_matrix']).sum()
        # TODO: Add MatrixNormalInverseWishart prior
        return lp

    # Expectation-maximization (EM) code
    def _zeros_like_suff_stats(self):
        return dict(sum_w=jnp.zeros((self.num_states)),
                    sum_x=jnp.zeros((self.num_states, self.covariate_dim)),
                    sum_y=jnp.zeros((self.num_states, self.emission_dim)),
                    sum_xxT=jnp.zeros((self.num_states, self.covariate_dim, self.covariate_dim)),
                    sum_xyT=jnp.zeros((self.num_states, self.covariate_dim, self.emission_dim)),
                    sum_yyT=jnp.zeros((self.num_states, self.emission_dim, self.emission_dim)))

    def _compute_expected_suff_stats(self, params, emissions, expected_states, covariates):
        sum_w = jnp.einsum("tk->k", expected_states)
        sum_x = jnp.einsum("tk,ti->ki", expected_states, covariates)
        sum_y = jnp.einsum("tk,ti->ki", expected_states, emissions)
        sum_xxT = jnp.einsum("tk,ti,tj->kij", expected_states, covariates, covariates)
        sum_xyT = jnp.einsum("tk,ti,tj->kij", expected_states, covariates, emissions)
        sum_yyT = jnp.einsum("tk,ti,tj->kij", expected_states, emissions, emissions)
        return dict(sum_w=sum_w, sum_x=sum_x, sum_y=sum_y, sum_xxT=sum_xxT, sum_xyT=sum_xyT, sum_yyT=sum_yyT)

    def _m_step_emissions(self, params, param_props, emission_stats):
        def _single_m_step(stats):
            # Unpack stats
            sum_w = stats['sum_w']
            sum_x = stats['sum_x']
            sum_y = stats['sum_y']
            sum_xxT = stats['sum_xxT']
            sum_xyT = stats['sum_xyT']
            sum_yyT = stats['sum_yyT']

            # Make block matrices for stacking features (x) and bias (1)
            sum_x1x1T = jnp.block(
                [[sum_xxT,                   jnp.expand_dims(sum_x, 1)],
                 [jnp.expand_dims(sum_x, 0), jnp.expand_dims(sum_w, (0, 1))]]
            )
            sum_x1yT = jnp.vstack([sum_xyT, sum_y])

            # Solve for the optimal A, b, and Sigma
            Ab = jnp.linalg.solve(sum_x1x1T, sum_x1yT).T
            Sigma = 1 / sum_w * (sum_yyT - Ab @ sum_x1yT)
            Sigma = 0.5 * (Sigma + Sigma.T)                 # for numerical stability
            return Ab[:, :-1], Ab[:, -1], Sigma

        As, bs, Sigmas = vmap(_single_m_step)(emission_stats)
        params["emissions"]["weights"] = As
        params["emissions"]["biases"] = bs
        params["emissions"]["covs"] = Sigmas
        return params
