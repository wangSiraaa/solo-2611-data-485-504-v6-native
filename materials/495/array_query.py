"""Relation operators accepting already constructed array values."""
def quantify(value,array,*,mode='any'):
    if array is None:return None
    comparisons=[value==item for item in array.values]
    return any(comparisons) if mode=='any' else all(comparisons)

def unnest(rows,fields,*,outer=False):
    result=[]
    for row in rows:
        arrays=[row[field].values if row[field] is not None else [] for field in fields]
        for ordinal,items in enumerate(zip(*arrays),1):
            result.append(dict(source=row['id'],ordinality=ordinal,values=list(items)))
    return result
