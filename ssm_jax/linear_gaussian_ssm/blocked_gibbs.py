import jax.numpy as jnp
import jax.random as jr
from jax import lax

from ssm_jax.linear_gaussian_ssm.inference import LGSSMParams, lgssm_posterior_sample
from ssm_jax.distributions import NormalInverseWishart as NIW, MatrixNormalInverseWishart as MNIW
from ssm_jax.distributions import niw_posterior_update, mniw_posterior_update


def blocked_gibbs(key, sample_size, emissions, D_hid, inputs=None, priors=None):
    """Estimation using blocked-Gibbs sampler
    
    Assume that parameters are fixed over time

    Args:
        key:          an object of jax.random.PRNGKey
        sample_size:  number of samples from the Gibbs sampler
        emissions:    a sequence of observations 
        priors:       a tuple containing prior distributions of the model 
                      priors = (initial_prior, 
                                dynamics_prior, 
                                emission_prior), 
                      where 
                      initial_prior is an NIW object
                      dynamics_prior is an MNIW object
                      emission_prior is an MNIW object
        D_hid:        dimension of hidden state
        inputs:       has shape (num_timesteps, dim_input) or None
    """
    num_timesteps = len(emissions)
    D_obs = emissions.shape[1]
    if inputs is None:
        inputs = jnp.zeros((num_timesteps, 0))
    D_in = inputs.shape[1]
    
    # set default priors 
    scale_obs = jnp.std(emissions, axis=0).mean()
    if priors is None:
        initial_prior = dynamics_prior = emission_prior = None
    else: 
        initial_prior, dynamics_prior, emission_prior = priors
    if initial_prior is None:
        initial_mean = jnp.ones(D_hid) * emissions[0].mean()
        initial_prior = NIW(loc=initial_mean,
                            mean_concentration=1.,
                            df=D_hid,
                            scale=5.*scale_obs*jnp.eye(D_hid))
    if dynamics_prior is None:
        F_init = jnp.ones((D_hid, D_hid)) / D_hid
        B_init = 0.1 * jr.uniform(key, shape=[D_hid, D_in])
        dynamics_prior = MNIW(loc=jnp.hstack((F_init, B_init)), 
                              col_precision=jnp.eye(D_hid+D_in),
                              df=D_hid, 
                              scale=jnp.eye(D_hid))
    if emission_prior is None:
        H_init = jnp.ones((D_obs, D_hid)) / D_hid
        D_init = 0.1 * jr.uniform(key, shape=[D_obs, D_in])
        emission_prior = MNIW(loc=jnp.hstack((H_init, D_init)), 
                              col_precision=jnp.eye(D_hid+D_in),
                              df=D_obs, 
                              scale=jnp.eye(D_obs))
    
    def log_prior_prob(params):
        """log probability of the model parameters under the prior distributions

        Args:
            params: model parameters

        Returns:
            log probability
        """
        # log prior probability of the initial state
        lp_init = initial_prior.log_prob((params.initial_covariance,
                                          params.initial_mean))
        
        # log prior probability of the dynamics
        lp_dyn = dynamics_prior.log_prob((params.dynamics_covariance,
                                          jnp.hstack((params.dynamics_matrix, 
                                                      params.dynamics_input_weights))
                                          ))
        
        # log prior probability of the emission
        lp_ems = emission_prior.log_prob((params.emission_covariance,
                                          jnp.hstack((params.emission_matrix, 
                                                      params.emission_input_weights))
                                          ))
        
        return lp_init + lp_dyn + lp_ems 
    
    def sufficient_stats_from_sample(states):
        """Convert samples of states to sufficient statistics
        
        Returns:
            (initial_stats, dynamics_stats, emission_stats)
        """
        # let xn[t] = x[t+1]          for t = 0...T-2
        x, xp, xn = states, states[:-1], states[1:]
        u, up= inputs, inputs[:-1]
        y = emissions

        init_stats = (x[0], jnp.outer(x[0], x[0]), 1)
        
        # quantities for the dynamics distribution
        # let zp[t] = [x[t], u[t]] for t = 0...T-2
        sum_zpzpT = jnp.block([[xp.T @ xp,  xp.T @ up],
                               [up.T @ xp,  up.T @ up]])
        sum_zpxnT = jnp.block([[xp.T @ xn],
                               [up.T @ xn]])
        sum_xnxnT = xn.T @ xn
        dynamics_stats = (sum_zpzpT, sum_zpxnT, sum_xnxnT, num_timesteps-1)
        
        # quantities for the emissions
        # let z[t] = [x[t], u[t]] for t = 0...T-1
        sum_zzT = jnp.block([[x.T @ x,  x.T @ u],
                             [u.T @ x,  u.T @ u]])
        sum_zyT = jnp.block([[x.T @ y],
                             [u.T @ y]])
        sum_yyT = y.T @ y
        emission_stats = (sum_zzT, sum_zyT, sum_yyT, num_timesteps)
        
        return init_stats, dynamics_stats, emission_stats
        
    def lgssm_params_sample(rng, init_stats, dynamics_stats, emission_stats):
        """Sample parameters of the model.
        """
        rngs = iter(jr.split(rng, 3))
        
        # Sample the initial params
        initial_posterior = niw_posterior_update(initial_prior, init_stats)
        S, m = initial_posterior.sample(seed=next(rngs))
        
        # Sample the dynamics params
        dynamics_posterior = mniw_posterior_update(dynamics_prior, dynamics_stats)
        Q, FB = dynamics_posterior.sample(seed=next(rngs))
        F, B = FB[:, :D_hid], FB[:, D_hid:] 
        
        # Sample the emission params
        emission_posterior = mniw_posterior_update(emission_prior, emission_stats)
        R, HD = emission_posterior.sample(seed=next(rngs))
        H, D = HD[:, :D_hid], HD[:, D_hid:]
        
        return LGSSMParams(initial_mean = m,
                           initial_covariance = S,
                           dynamics_matrix = F,
                           dynamics_input_weights = B,
                           dynamics_covariance = Q,
                           emission_matrix = H,
                           emission_input_weights = D,
                           emission_covariance = R)
    
    def one_sample(params, rng):
        """One complete iteration of the blocked Gibbs sampler
        """
        rngs = jr.split(rng, 2)
        l_prior = log_prior_prob(params)
        ll, states = lgssm_posterior_sample(rngs[0], params, emissions, inputs)
        
        # Compute sufficient statistics for parameters
        _stats = sufficient_stats_from_sample(states)
        
        # Sample parameters
        params_new = lgssm_params_sample(rngs[1], *_stats)
        
        # Compute the log probability
        log_probs = l_prior + ll
        
        return params_new, (params, log_probs)
    
    # Initialize the initial state
    S_0, m_0 = initial_prior.mode()
    
    # Initialize the dynamics parameters
    Q_0, FB_0 = dynamics_prior.mode()
    F_0, B_0 = FB_0[:, :D_hid], FB_0[:, D_hid:]

    # Initialize the emission parameters
    R_0, HD_0 = emission_prior.mode()
    H_0, D_0 = HD_0[:, :D_hid], HD_0[:, D_hid:]
    
    params_0 = LGSSMParams(initial_mean = m_0,
                           initial_covariance = S_0,
                           dynamics_matrix = F_0,
                           dynamics_input_weights = B_0,
                           dynamics_covariance = Q_0,
                           emission_matrix = H_0,
                           emission_input_weights = D_0,
                           emission_covariance = R_0)
    
    # Sample
    keys = jr.split(key, sample_size)
    _, samples_and_log_probs = lax.scan(one_sample, params_0, keys)
    samples_of_parameters, log_probs = samples_and_log_probs
        
    return log_probs, samples_of_parameters
