import jax.numpy as jnp
from jax import lax
from jax.tree_util import tree_leaves, tree_reduce, tree_map, register_pytree_node_class
import tensorflow_probability.substrates.jax.bijectors as tfb
from typing import Optional


@register_pytree_node_class
class ParameterProperties:
    """A simple wrapper for mutable parameter properties.

    Note: the properties are stored in the aux_data of this PyTree so that
    changes will trigger recompilation of functions that rely on them.

    Args:
        trainable (bool): flat specifying whether or not to fit this parameter
        constrainer (Optional tfb.Bijector): bijector mapping to constrained form
    """
    def __init__(self,
                 trainable: bool = True,
                 constrainer: Optional[tfb.Bijector] = None) -> None:
        self.trainable = trainable
        self.constrainer = constrainer

    def tree_flatten(self):
        return (), (self.trainable, self.constrainer)

    @classmethod
    def tree_unflatten(cls, aux_data, children):
        return cls(*aux_data)

# @dataclass
# class ParameterProperties:
#     trainable: bool = True
#     constrainer: Optional[tfb.Bijector] = None


def to_unconstrained(params, props):
    """Extract the trainable parameters and convert to unconstrained, then return
    unconstrained parameters and fixed parameters.

    Args:
        params (dataclass): (nested) dataclass whose leaf values are DeviceArrays containing
                              parameter values.
        props (dict): matching (nested) dictionary whose leaf values are ParameterProperties

    Returns:
        unc_params (dict): (nested) dictionary whose values are the unconstrainend parameter
                            values, but only for the parameters that are marked trainable in
                            `param_props`.
        params (dataclass): the original `params` input.
    """
    to_unc = lambda value, prop: prop.constrainer.inverse(value) \
        if prop.constrainer is not None else value
    is_leaf = lambda node: isinstance(node, (ParameterProperties,))
    return tree_map(to_unc, params, props, is_leaf=is_leaf)


def from_unconstrained(unc_params, props):
    """Convert the unconstrained parameters to constrained form and
    combine them with the original parameters.

    Args:
        unc_params: PyTree whose leaf values are DeviceArrays
        props: matching PyTree whose leaf values are ParameterProperties

    Returns:
        params:
    """
    def from_unc(unc_value, prop):
        value = prop.constrainer(unc_value) if prop.constrainer is not None else unc_value
        value = lax.stop_gradient(value) if not prop.trainable else value
        return value

    is_leaf = lambda node: isinstance(node, (ParameterProperties,))
    return tree_map(from_unc, unc_params, props, is_leaf=is_leaf)


def log_det_jac_constrain(unc_params, props):
    """Log determinant of the Jacobian matrix evaluated at the unconstrained parameters.
    """
    def _compute_logdet(unc_value, prop):
        logdet = prop.constrainer.forward_log_det_jacobian(unc_value).sum() \
            if prop.constrainer is not None else 0.0
        return logdet if prop.trainable else 0.0

    is_leaf = lambda node: isinstance(node, (ParameterProperties,))
    logdets = tree_map(_compute_logdet, unc_params, props, is_leaf=is_leaf)
    return tree_reduce(jnp.add, logdets, 0.0)
