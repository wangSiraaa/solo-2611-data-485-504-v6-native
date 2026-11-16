import json
from pathlib import Path
from query import grouped
from units import Registry, Quantity

registry = Registry()
data = json.loads(Path(__file__).with_name('examples.json').read_text())
print('length:', registry.convert(Quantity('100', 'cm'), 'm').record())
print('temperature delta:', registry.convert(Quantity('18', 'F', 'delta'), 'C').record())
for name, kind in [('temperature_points', 'point'), ('temperature_deltas', 'delta')]:
    print(name, grouped(registry, data[name], group_by=['site'], value_field='reading',
                        unit_field='unit', kind=kind, aggregate='mean', output_unit='C'))
