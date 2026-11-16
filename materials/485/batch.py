"""Prototype sequential data import batch with response references."""
from store import StoreError


def _field(value, results):
    if isinstance(value, dict) and '$result' in value:
        return results[value['$result']][value['field']]
    return value


def run(store, requests):
    results, responses = {}, []
    for request in requests:
        try:
            key = _field(request['key'], results)
            operation = request['op']
            if operation == 'create':
                result = store.create(key, request['body']['value'])
            elif operation == 'update':
                result = store.update(key, request['body']['value'],
                                      _field(request.get('if_match'), results))
            elif operation == 'delete':
                result = store.delete(key, _field(request.get('if_match'), results))
            elif operation == 'read':
                result = store.read(key)
            else:
                raise StoreError('BAD_OPERATION')
            results[request['id']] = result
            responses.append(dict(id=request['id'], status='ok', result=result))
        except Exception as exc:
            store.db.rollback()
            responses.append(dict(id=request['id'], status='failed',
                                  error=getattr(exc, 'code', type(exc).__name__)))
    return responses
