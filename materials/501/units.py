"""Small quantity exchange SDK; the prototype treats every conversion as affine."""
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Unit:
    dimension: str
    scale: Fraction
    offset: Fraction = Fraction(0)


@dataclass(frozen=True)
class Quantity:
    value: Fraction
    unit: str
    kind: str = 'delta'

    def __post_init__(self):
        object.__setattr__(self, 'value', Fraction(self.value))

    def record(self):
        return dict(value=str(self.value), unit=self.unit, kind=self.kind)


class QuantityError(ValueError):
    pass


class Registry:
    def __init__(self):
        self.units = {
            'm': Unit('length', Fraction(1)),
            'cm': Unit('length', Fraction(1, 100)),
            'K': Unit('temperature', Fraction(1)),
            'C': Unit('temperature', Fraction(1), Fraction(27315, 100)),
            'F': Unit('temperature', Fraction(5, 9), Fraction(45967, 180)),
        }

    def register(self, name, dimension, scale, offset='0'):
        scale = Fraction(scale)
        if name in self.units or scale <= 0:
            raise QuantityError('invalid unit registration')
        self.units[name] = Unit(dimension, scale, Fraction(offset))

    def convert(self, quantity, target):
        source, dest = self.units[quantity.unit], self.units[target]
        if source.dimension != dest.dimension:
            raise QuantityError('incompatible dimensions')
        base = quantity.value * source.scale + source.offset
        return Quantity((base - dest.offset) / dest.scale, target, quantity.kind)

    def add(self, left, right):
        converted = self.convert(right, left.unit)
        return Quantity(left.value + converted.value, left.unit, left.kind)

    def subtract(self, left, right):
        converted = self.convert(right, left.unit)
        return Quantity(left.value - converted.value, left.unit, left.kind)
