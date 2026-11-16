import json
from pathlib import Path
import tempfile
from validator import ProgramRegistry, validate
from vm import run

case = json.loads(Path(__file__).with_name('cases.json').read_text())
schema = case['schema']
out = {'normal_static': validate(case['add'],schema),
       'normal_external_vm': run(case['add'],[{'x':3,'flag':False}])}
with tempfile.TemporaryDirectory() as folder:
    registry = ProgramRegistry(folder)
    receipt = registry.admit('unsafe-without-any-rows',case['unsafe_merge'],schema)
    out['offline_registry_failure'] = {'actual_accepted':receipt['analysis']['accepted'],
        'saved_artifact':Path(folder,'unsafe-without-any-rows.json').exists(),
        'expected':'reject: true branch has text at pc6 where false branch has int'}
out['growing_backedge_failure'] = {'actual':validate(case['growing_backedge'],schema),
    'expected':'reject incompatible entry stack heights0 and1 at pc0'}
out['valid_nullable_vm'] = {'actual':run(case['add'],[{'x':None,'flag':False}]),
    'expected_values':[None]}
out['valid_external_divide_vm'] = {'actual':run(case['divide'],[
    {'x':2,'flag':False},{'x':0,'flag':False},{'x':None,'flag':False},{'x':5,'flag':False}]),
    'expected_rows':[{'value':5},{'error':'division_by_zero'},{'value':None},{'value':2}]}
out['safe_branch_avoids_divide_zero'] = run(case['safe_branch'],[{'x':0,'flag':True}])
out['per_row_fuel'] = {'actual':run(case['add'],[{'x':1,'flag':False},{'x':2,'flag':False}],fuel=4),
    'expected_values':[3,4]}
out['actual_admission_execution_coupling'] = {'bad_branch_run':run(case['unsafe_merge'],[{'x':1,'flag':True}]),
    'expected':'offline admission prevents publishing this well-formed but type-unsafe program before any dataset arrives'}
print(json.dumps(out,indent=2))
