"""Offline overload catalog: returns plans without evaluating expressions."""
from copy import deepcopy

class BindError(ValueError):
    pass

class FunctionCatalog:
    def __init__(self,definitions):
        self.definitions=deepcopy(definitions)

    def resolve(self,name,arguments):
        candidates=[f for f in self.definitions if f['name']==name]
        for fn in candidates:
            required=sum('default' not in p for p in fn['parameters'])
            if required<=len(arguments)<=len(fn['parameters']):
                bindings=[]
                for index,param in enumerate(fn['parameters']):
                    if index<len(arguments):bindings.append(dict(arg=index,cast=param['type']))
                    else:bindings.append(dict(default=param['default'],cast=param['type']))
                return dict(function=deepcopy(fn),arguments=deepcopy(arguments),bindings=bindings)
        raise BindError('no matching function')
