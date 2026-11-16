import json
from pathlib import Path
from policy import evaluate
from recipes import execute, plan_and_deliver

cases = json.loads(Path(__file__).with_name('cases.json').read_text())
results = {}
for name, case in cases.items():
    results[name] = {'actual': plan_and_deliver(case['graph'], case['root'], case['purpose'])}
results['public_delivery']['expected'] = {'status': 'ok', 'cost': 6,
    'bindings': {'choose': 'open'}, 'rows': [{'id': 1, 'value': 'open', '_sources': ['open']}]}
results['shared_cost']['expected'] = {'cost': 7, 'row_count': 2,
    'reason': 'data, shared, bundle charged once; concat preserves two input occurrences'}
results['join_conflict']['expected'] = {'allowed': False,
    'reason': 'join derives a group containing both reciprocal and private contributions'}
results['collection_not_derivative']['expected'] = {'allowed': True,
    'reason': 'concat retains separate contribution groups and is not a derivation'}
results['ordinary_join']['expected'] = {'status': 'ok', 'cost': 4,
    'rows': [{'id': 1, 'left': 'x', 'right': 'y', '_sources': ['a', 'b']}]}
# A procurement customer never executes tables or chooses alternatives.
case = cases['join_conflict']
results['direct_procurement'] = {'actual': evaluate(case['graph'], case['root'], {}, case['purpose']),
    'expected_allowed': False}
# Maintenance builds already-bound recipes with no policy decision.
case = cases['shared_cost']
rows, cost = execute(case['graph'], case['root'], {})
results['direct_recipe_execution'] = {'actual_rows': rows, 'actual_cost': cost, 'expected_cost': 7,
    'expected_row_count': 2}
print(json.dumps(results, ensure_ascii=False, indent=2))

