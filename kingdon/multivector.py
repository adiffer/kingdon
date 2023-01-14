from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import reduce, cached_property
from typing import Generator
from itertools import product

from sympy import Symbol, Expr, sympify

from kingdon.codegen import _lambdify_mv


@dataclass(init=False)
class MultiVector:
    algebra: "Algebra"
    _values: tuple = field(default_factory=tuple)
    _keys: tuple = field(default_factory=tuple)

    def __new__(cls, algebra, values=None, keys=None, *, name=None, grades=None, symbolcls=Symbol):
        """
        :param algebra: Instance of :class:`~kingdon.algebra.Algebra`.
        :param keys: Keys corresponding to the basis blades in binary rep.
        :param values: Values of the multivector. If keys are provided, then keys and values should
            satisfy :code:`len(keys) == len(values)`. If no keys nor grades are provided, :code:`len(values)`
            should equal :code:`len(algebra)`, i.e. a full multivector. If grades is provided,
            then :code:`len(values)` should be identical to the number of values in a multivector
            of that grade.
        :param name: Base string to be used as the name for symbolic values.
        :param grades: Optional, :class:`tuple` of grades in this multivector.
            If present, :code:`keys` is checked against these grades.
        :param symbolcls: Optional, class to be used for symbol creation. This is a :class:`sympy.Symbol` by default,
            but could be e.g. :class:`symfit.Variable` or :class:`symfit.Parameter` when the goal is to use this
            multivector in a fitting problem.
        """
        # Sanitize input
        values = values if values is not None else tuple()
        name = name if name is not None else ''
        if grades is not None:
            if not all(0 <= grade <= algebra.d for grade in grades):
                raise ValueError(f'Each grade in `grades` needs to be a value between 0 and {algebra.d}.')
        else:
            grades = tuple(range(algebra.d + 1))

        # Construct a new MV on the basis of the kind of input we received.
        if isinstance(values, Mapping):
            keys, values = zip(*values.items())
        elif len(values) == len(algebra) and not keys:
            keys = tuple(range(len(values)))
        elif len(values) == len(algebra.indices_for_grades[grades]) and not keys:
            keys = algebra.indices_for_grades[grades]
        elif name and not values:
            # values was not given, but we do have a name. So we are in symbolic mode.
            keys = algebra.indices_for_grades[grades] if not keys else keys
            values = tuple(symbolcls(f'{name}{algebra.bin2canon[k][1:]}') for k in keys)
        elif len(keys) != len(values):
            raise TypeError(f'Length of `keys` and `values` have to match.')

        if not all(isinstance(k, int) for k in keys):
            keys = tuple(key if key in algebra.bin2canon else algebra.canon2bin[key]
                         for key in keys)
        if any(isinstance(v, str) for v in values):
            values = tuple(val if not isinstance(val, str) else sympify(val)
                           for val in values)

        if not set(keys) <= set(algebra.indices_for_grades[grades]):
            raise ValueError(f"All keys should be of grades {grades}.")

        return cls.fromkeysvalues(algebra, keys, values)

    @classmethod
    def fromkeysvalues(cls, algebra, keys, values):
        """
        Initiate a multivector from a sequence of keys and a sequence of values.
        """
        obj = object.__new__(cls)
        obj.algebra = algebra
        obj._values = values
        obj._keys = keys
        return obj

    @classmethod
    def frommatrix(cls, algebra, matrix):
        """
        Initiate a multivector from a matrix. This matrix is assumed to be
        generated by :class:`~kingdon.multivector.MultiVector.asmatrix`, and
        thus we only read the first column of the input matrix.
        """
        obj = cls(algebra=algebra, values=matrix[:, 0])
        return obj

    def keys(self):
        return self._keys

    def values(self):
        return self._values

    def items(self):
        return zip(self._keys, self._values)

    def __len__(self):
        return len(self._values)

    def itermv(self, axis=None) -> Generator["MultiVector", None, None]:
        """
        Returns an iterator over the multivectors within this multivector.

        :param axis: Axis over which to iterate. Default is to iterate over all possible mv.
        """
        shape = self.shape()[1:]
        if not shape:
            return self
        elif axis is None:
            return (
                MultiVector.fromkeysvalues(self.algebra, keys=self.keys(), values=self[(slice(None), *indices)])
                for indices in product(*(range(n) for n in shape))
            )
        else:
            raise NotImplementedError

    def shape(self):
        """ Return the shape of the .values() attribute of this multivector. """
        if hasattr(self._values, 'shape'):
            return self._values.shape
        elif hasattr(self._values[0], 'shape'):
            return len(self), *self._values[0].shape
        else:
            return len(self),

    @cached_property
    def grades(self):
        """ Tuple of the grades present in `self`. """
        return tuple(sorted({bin(ind).count('1') for ind in self.keys()}))

    def grade(self, grades):
        """
        Returns a new  :class:`~kingdon.multivector.MultiVector` instance with
        only the selected `grades` from `self`.

        :param grades: tuple or int, grades to select.
        """
        if isinstance(grades, int):
            grades = (grades,)
        elif not isinstance(grades, tuple):
            grades = tuple(grades)

        vals = {k: self[k]
                for k in self.algebra.indices_for_grades[grades] if k in self.keys()}
        return self.fromkeysvalues(self.algebra, tuple(vals.keys()), tuple(vals.values()))

    @cached_property
    def issymbolic(self):
        """ True if this mv contains Symbols, False otherwise. """
        return any(isinstance(v, Expr) for v in self.values())

    def __neg__(self):
        try:
            values = - self.values()
        except TypeError:
            values = tuple(-v for v in self.values())
        return self.fromkeysvalues(self.algebra, self.keys(), values)

    def __invert__(self):  # reversion
        values = tuple((-1)**(bin(k).count("1") // 2) * v for k, v in self.items())
        return self.fromkeysvalues(self.algebra, self.keys(), values)

    def normsq(self):
        return self.algebra.normsq(self)

    def normalized(self):
        """ Normalized version of this multivector. """
        normsq = self.normsq()
        if normsq.grades == (0,):
            return self / normsq[0] ** 0.5
        else:
            raise NotImplementedError

    def inv(self):
        """ Inverse of this multivector. """
        return self.algebra.inv(self)

    def __add__(self, other):
        if not isinstance(other, MultiVector):
            other = self.fromkeysvalues(self.algebra, (0,), (other,))
        vals = dict(self.items())
        for k, v in other.items():
            if k in vals:
                vals[k] += v
            else:
                vals[k] = v
        return self.fromkeysvalues(self.algebra, tuple(vals.keys()), tuple(vals.values()))

    def __sub__(self, other):
        return self + (-other)

    def __truediv__(self, other):
        return self.algebra.div(self, other)

    def __str__(self):
        if len(self.values()):
            canon_sorted_vals = sorted(self.items(), key=lambda x: (len(self.algebra.bin2canon[x[0]]), self.algebra.bin2canon[x[0]]))
            return ' + '.join([f'({val}) * {self.algebra.bin2canon[key]}' for key, val in canon_sorted_vals])
        else:
            return '0'

    def __getitem__(self, item):
        if isinstance(item, tuple):
            key, *subslices = item
        else:
            key, subslices = item, tuple()

        # TODO: We could turn slices into the valid range in binary rep.
        #  This is complicated by the fact that the binary keys do not
        #  form a consecutive range.
        if not isinstance(key, slice):
            # Convert key from a basis-blade in binary rep to a valid index in values.
            key = key if key in self.algebra.bin2canon else self.algebra.canon2bin[key]
            try:
                key = self.keys().index(key)
            except ValueError:
                return 0

        values = self.values()
        if isinstance(values, (tuple, list)):
            keys = [key] if key != slice(None) else [self.keys().index(k) for k in self.keys()]
            return_values = []
            for key in keys:
                return_values.append(values[key])
                for subslice in subslices:
                    return_values[key] = return_values[key][subslice]
            if len(keys) == 1:
                return_values = return_values[0]
        else:
            return_values = values[(key, *subslices)]
        return return_values

    def __contains__(self, item):
        item = item if item in self.algebra.bin2canon else self.algebra.canon2bin[item]
        return item in self._keys

    @cached_property
    def free_symbols(self):
        return reduce(lambda tot, x: tot | x, (v.free_symbols for v in self.values()))

    @cached_property
    def _callable(self):
        """ Return the callable function for this MV. """
        return _lambdify_mv(sorted(self.free_symbols, key=lambda x: x.name), self)

    def __call__(self, *args, **kwargs):
        if not self.free_symbols:
            return self
        keys_out, func = self._callable
        values = func(*args, **kwargs)
        return self.fromkeysvalues(self.algebra, keys_out, values)

    def asmatrix(self):
        """ Returns a matrix representation of this multivector. """
        return sum(v * self.algebra.matrix_basis[k] for k, v in self.items())

    def gp(self, other):
        return self.algebra.gp(self, other)

    __mul__ = __rmul__ = gp

    def conj(self, other):
        """ Apply `x := self` to `y := other` under conjugation: `x*y*~x`. """
        return self.algebra.conj(self, other)

    def proj(self, other):
        """
        Project :code:`x := self` onto :code:`y := other`: :code:`x @ y = (x | y) * ~y`.
        For correct behavior, :code:`x` and :code:`y` should be normalized (k-reflections).
        """
        return self.algebra.proj(self, other)

    __matmul__ = proj

    def cp(self, other):
        """
        Calculate the commutator product of :code:`x := self` and :code:`y := other`:
        :code:`x.cp(y) = 0.5*(x*y-y*x)`.
        """
        return self.algebra.cp(self, other)

    def acp(self, other):
        """
        Calculate the anti-commutator product of :code:`x := self` and :code:`y := other`:
        :code:`x.cp(y) = 0.5*(x*y+y*x)`.
        """
        return self.algebra.acp(self, other)

    def ip(self, other):
        return self.algebra.ip(self, other)

    __or__ = ip

    def op(self, other):
        return self.algebra.op(self, other)

    __xor__ = __rxor__ = op

    def lc(self, other):
        return self.algebra.lc(self, other)

    __lshift__ = lc

    def rc(self, other):
        return self.algebra.rc(self, other)

    __rshift__ = rc

    def sp(self, other):
        """ Scalar product: :math:`\langle x \cdot y \rangle`. """
        return self.algebra.sp(self, other)

    def rp(self, other):
        return self.algebra.rp(self, other)

    __and__ = rp

    def __pow__(self, power, modulo=None):
        # TODO: this should also be taken care of via codegen, but for now this workaround is ok.
        if power == 2:
            return self.algebra.gp(self, self)
        else:
            raise NotImplementedError

    def outerexp(self):
        return self.algebra.outerexp(self)


    def dual(self, kind='auto'):
        """
        Compute the dual of `self`. There are three different kinds of duality in common usage.
        The first is polarity, which is simply multiplying by the inverse PSS. This is the only game in town for
        non-degenerate metrics (Algebra.r = 0). However, for degenerate spaces this no longer works, and we have
        two popular options: Poincaré and Hodge duality.

        By default, :code:`kingdon` will use polarity in non-degenerate spaces, and Hodge duality for spaces with
        `Algebra.r = 1`. For spaces with `r > 2`, little to no literature exists, and you are on your own.

        :param kind: if 'auto' (default), :code:`kingdon` will try to determine the best dual on the
            basis of the signature of the space. See explenation above.
            To ensure polarity, use :code:`kind='polarity'`, and to ensure Hodge duality,
            use :code:`kind='hodge'`.
        """
        if kind == 'polarity' or kind == 'auto' and self.algebra.r == 0:
            return self / self.algebra.pss
        elif kind == 'hodge' or kind == 'auto' and self.algebra.r == 1:
            return self.algebra.multivector(
                {len(self.algebra) - 1 - eI: self.algebra.signs[eI, len(self.algebra) - 1 - eI] * val
                 for eI, val in self.items()}
            )
        elif kind == 'auto':
            raise Exception('Cannot select a suitable dual in auto mode for this algebra.')
        else:
            raise ValueError(f'No dual found for kind={kind}.')

    def undual(self, kind='auto'):
        """
        Compute the undual of `self`. See :class:`~kingdon.multivector.MultiVector.dual` for more information.
        """
        if kind == 'polarity' or kind == 'auto' and self.algebra.r == 0:
            return self * self.algebra.pss
        elif kind == 'hodge' or kind == 'auto' and self.algebra.r == 1:
            return self.algebra.multivector(
                {len(self.algebra) - 1 - eI: self.algebra.signs[len(self.algebra) - 1 - eI, eI] * val
                 for eI, val in self.items()}
            )
        elif kind == 'auto':
            raise Exception('Cannot select a suitable undual in auto mode for this algebra.')
        else:
            raise ValueError(f'No undual found for kind={kind}.')


# class GradedMultiplication:
#     def _binary_operation(self, other, func_dictionary, codegen):
#         """ Helper function for all multiplication types such as gp, sp, cp etc. """
#         if self.algebra != other.algebra:
#             raise AlgebraError("Cannot multiply elements of different algebra's.")
#
#         keys_in = (self.algebra.indices_for_grades[self.grades],
#                    self.algebra.indices_for_grades[other.grades])
#         if keys_in not in func_dictionary:
#             x = self.algebra.multivector(vals={ek: Symbol(f'a{self.algebra.bin2canon[ek][1:]}')
#                                                for ek in keys_in[0]})
#             y = self.algebra.multivector(vals={ek: Symbol(f'b{self.algebra.bin2canon[ek][1:]}')
#                                                for ek in keys_in[1]})
#             keys_out, func = func_dictionary[keys_in] = codegen(x, y)
#         else:
#             keys_out, func = func_dictionary[keys_in]
#
#         args = chain((self.vals.get(i, 0) for i in keys_in[0]),
#                      (other.vals.get(i, 0) for i in keys_in[1]))
#         res_vals = defaultdict(int, {k: v for k, v in zip(keys_out, func(*args))
#                                      if (True if v.__class__ is not Expr else simplify(v))})
#
#         return self.algebra.mvfromtrusted(vals=res_vals)
