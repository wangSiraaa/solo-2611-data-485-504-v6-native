"""Mutable SQL-shaped arrays used by standalone import clients."""
from copy import deepcopy

class ArrayError(ValueError):
    pass

class ArrayValue:
    def __init__(self,lowers,shape,values):
        self.lowers=list(lowers);self.shape=list(shape);self.values=list(values)

    def record(self):
        return deepcopy(dict(lowers=self.lowers,shape=self.shape,values=self.values))

    def at(self,indexes):
        offset=0
        for index,size in zip(indexes,self.shape):offset=offset*size+(index-1)
        try:return self.values[offset]
        except IndexError:return None

    def slice(self,bounds):
        if len(self.shape)!=1:raise ArrayError('prototype only slices one dimension')
        lo,hi=bounds[0];start=lo-self.lowers[0];stop=hi-self.lowers[0]+1
        values=self.values[max(0,start):max(0,stop)]
        return ArrayValue(self.lowers,[len(values)],values)

    def assign_slice(self,bounds,replacement):
        if len(self.shape)!=1:raise ArrayError('prototype only assigns one dimension')
        lo,hi=bounds[0];start=lo-self.lowers[0]
        for i,value in enumerate(replacement.values):
            if value is not None and type(value) is not int:raise ArrayError('invalid element')
            self.values[start+i]=value
