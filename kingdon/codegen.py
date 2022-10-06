from itertools import product, repeat, combinations, chain
from collections import defaultdict
import inspect
import os
import pickle
from concurrent.futures import ProcessPoolExecutor

from sympy import simplify
from sympy.utilities.lambdify import lambdify
from numba import njit

def codegen_gp(x, y, symbolic=False):
    """
    Generate the geometric product between `x` and `y`.

    :x: MultiVector
    :y: MultiVector
    :symbolic: If true, return a dict of symbolic expressions instead of lambda functions.
    :return: dictionary with integer keys indicating the corresponding basis blade in binary convention,
        and values which are a 3-tuple of indices in `x`, indices in `y`, and a lambda function.
    """
    res_vals = defaultdict(int)
    for (ei, vi), (ej, vj) in product(x.vals.items(), y.vals.items()):
        if x.algebra.signs[ei, ej]:
            res_vals[ei ^ ej] += x.algebra.signs[(ei, ej)] * vi * vj
    # Remove expressions which are identical to zero
    res_vals = {k: simp_expr for k, expr in res_vals.items() if (simp_expr := simplify(expr))}
    if symbolic:
        return res_vals

    return _lambdify_binary(x, y, res_vals)

def codegen_conj(x, y, symbolic=False):
    """
    Generate the sandwich (conjugation) product between `x` and `y`: `x*y*~x`.

    :return: tuple of keys in binary representation and a lambda function.
    """
    # xyx = x*y*~x
    xy = x.algebra.multivector(vals=codegen_gp(x, y, symbolic=True))
    xyx = codegen_gp(xy, ~x, symbolic=True)
    if symbolic:
        return xyx

    return _lambdify_binary(x, y, xyx)

def codegen_cp(x, y):
    """
    Generate the commutator product of `x := self` and `y := other`: `x.cp(y) = 0.5*(x*y-y*x)`.

    :return: tuple of keys in binary representation and a lambda function.
    """
    xy = codegen_gp(x, y / 2, symbolic=True)
    yx = codegen_gp(y, x / 2, symbolic=True)
    for k, v in yx.items():
        if k in xy:
            if xy[k] - v:  # Symbolically not equal to zero
                xy[k] -= v
            else:
                del xy[k]
        else:
            xy[k] = - v
    return _lambdify_binary(x, y, xy)

def codegen_acp(x, y):
    """
    Generate the anti-commutator product of `x := self` and `y := other`: `x.acp(y) = 0.5*(x*y+y*x)`.

    :return: tuple of keys in binary representation and a lambda function.
    """
    return NotImplementedError

def codegen_ip(x, y, diff_func=abs, symbolic=False):
    """
    Generate the inner product of `x := self` and `y := other`.

    :param diff_func: How to treat the difference between the binary reps of the basis blades.
        if :code:`abs`, compute the symmetric inner product. When :code:`lambda x: -x` this
        function generates left-contraction, and when :code:`lambda x: x`, right-contraction.
    :return: tuple of keys in binary representation and a lambda function.
    """
    res_vals = defaultdict(int)
    for (ei, vi), (ej, vj) in product(x.vals.items(), y.vals.items()):
        if ei ^ ej == diff_func(ei - ej):
            res_vals[ei ^ ej] += x.algebra.signs[ei, ej] * vi * vj
    # Remove expressions which are identical to zero
    res_vals = {k: simp_expr for k, expr in res_vals.items() if (simp_expr := simplify(expr))}
    if symbolic:
        return res_vals

    return _lambdify_binary(x, y, res_vals)

def codegen_lc(x, y):
    """
    Generate the left-contraction of `x := self` and `y := other`.

    :return: tuple of keys in binary representation and a lambda function.
    """
    return codegen_ip(x, y, diff_func=lambda x: -x)

def codegen_rc(x, y):
    """
    Generate the right-contraction of `x := self` and `y := other`.

    :return: tuple of keys in binary representation and a lambda function.
    """
    return codegen_ip(x, y, diff_func=lambda x: x)

def codegen_sp(x, y):
    """
    Generate the scalar product of `x := self` and `y := other`.

    :return: tuple of keys in binary representation and a lambda function.
    """
    return codegen_ip(x, y, diff_func=lambda x: 0)

def codegen_proj(x, y):
    """
    Generate the projection of `x := self` onto `y := other`: :math:`(x \cdot y) / y`.

    :return: tuple of keys in binary representation and a lambda function.
    """
    x_dot_y = x.algebra.multivector(codegen_ip(x, y, symbolic=True))
    x_proj_y = codegen_gp(x_dot_y, ~y, symbolic=True)
    return _lambdify_binary(x, y, x_proj_y)

def codegen_op(x, y, symbolic=False):
    """
    Generate the outer product of `x := self` and `y := other`: `x.op(y) = x ^ y`.

    :x: MultiVector
    :y: MultiVector
    :return: dictionary with integer keys indicating the corresponding basis blade in binary convention,
        and values which are a 3-tuple of indices in `x`, indices in `y`, and a lambda function.
    """
    res_vals = defaultdict(int)
    for (ei, vi), (ej, vj) in product(x.vals.items(), y.vals.items()):
        if ei ^ ej == ei + ej:
            res_vals[ei ^ ej] += (-1)**x.algebra.swaps[ei, ej] * vi * vj
    # Remove expressions which are identical to zero
    res_vals = {k: simp_expr for k, expr in res_vals.items() if (simp_expr := simplify(expr))}
    if symbolic:
        return res_vals

    return _lambdify_binary(x, y, res_vals)

def codegen_rp(x, y):
    """
    Generate the commutator product of `x := self` and `y := other`: `x.cp(y) = 0.5*(x*y-y*x)`.

    :return: tuple of keys in binary representation and a lambda function.
    """
    x_regr_y = x.algebra.multivector(codegen_op(x.dual(), y.dual(), symbolic=True)).undual()
    return _lambdify_binary(x, y, x_regr_y.vals)


def codegen_inv(x):
    """
    Generate code for the inverse of :code:`x`.
    Currently, this always uses the Shirokov inverse, which is works in any algebra,
    but it can be expensive to compute.
    In the future this should be extended to use dedicated solutions for known cases.
    """
    k = 2 ** ((x.algebra.d + 1) // 2)
    x_i = x
    i = 1
    while x_i.grades != (0,) and x_i:
        c_i = k * x_i[0] / i
        adj_x = (x_i - c_i)
        adj_x = x.algebra.multivector({k: simplify(v) for k, v in adj_x.vals.items()})
        x_i = x * adj_x
        x_i = x.algebra.multivector({k: simplify(v) for k, v in x_i.vals.items()})
        i += 1
    xinv = adj_x / x_i[0]
    # xinv = x.algebra.multivector({k: simp_expr for k, v in xinv.vals.items() if (simp_expr := simplify(v))})
    return _lambdify_unary(x, xinv.vals)

def _lambdify_binary(x, y, vals):
    xy_symbols = [list(x.vals.values()), list(y.vals.values())]
    func = lambdify(xy_symbols, list(vals.values()), cse=x.algebra.cse)
    return tuple(vals.keys()), njit(func) if x.algebra.numba else func

def _lambdify_unary(x, vals):
    func = lambdify([list(x.vals.values())], list(vals.values()), cse=x.algebra.cse)
    return tuple(vals.keys()), njit(func) if x.algebra.numba else func

def _lambdify_mv(free_symbols, mv):
    # TODO: Numba wants a tuple in the line below, but simpy only produces a
    #  list as output if this is a list, not a tuple. See if we can solve this.
    func = lambdify(free_symbols, list(mv.vals.values()), cse=mv.algebra.cse)
    return tuple(mv.vals.keys()), njit(func) if mv.algebra.numba else func
