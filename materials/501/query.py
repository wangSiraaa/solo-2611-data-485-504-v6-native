"""Typed warehouse aggregate prototype over exchange quantities."""
from fractions import Fraction
from units import Quantity, QuantityError


def grouped(registry, rows, *, group_by, value_field, unit_field, kind,
            aggregate, output_unit):
    if aggregate not in ('sum', 'mean'):
        raise QuantityError('unknown aggregate')
    groups = {}
    for row in rows:
        key = tuple(row[name] for name in group_by)
        groups.setdefault(key, [])
        if row[value_field] is not None:
            q = Quantity(Fraction(row[value_field]), row[unit_field], kind)
            groups[key].append(registry.convert(q, output_unit).value)
    result = []
    for key, values in groups.items():
        total = sum(values, Fraction(0))
        value = total if aggregate == 'sum' else (total / len(values) if values else None)
        result.append(dict(group=list(key), value=None if value is None else str(value),
                           unit=output_unit, kind=kind, count=len(values)))
    return result
