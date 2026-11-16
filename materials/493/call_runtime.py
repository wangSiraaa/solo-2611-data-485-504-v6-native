"""Public bound-plan executor; callers may supply an independently produced plan."""
from decimal import Decimal

class CallError(ValueError):
    pass

def convert(value,target):
    if value is None:return None
    if target=='int':return int(value)
    if target=='decimal':return Decimal(float(value))
    if target=='text':return str(value)
    if target=='bool':return bool(value)
    raise CallError('unknown type')

class CallRuntime:
    def __init__(self,functions):
        self.functions=dict(functions)

    def invoke(self,plan,evaluate):
        values=[]
        for parameter,binding in zip(plan['function']['parameters'],plan['bindings']):
            value=(evaluate(plan['arguments'][binding['arg']]['expr']) if 'arg' in binding
                   else binding['default'])
            if value is None and 'default' in parameter:value=parameter['default']
            values.append(convert(value,binding['cast']))
        return self.functions[plan['function']['id']](*values)
