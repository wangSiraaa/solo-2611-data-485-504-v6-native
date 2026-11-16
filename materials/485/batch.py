"""Ordered, grouped batch orchestration for the conditional store.

``run(store, requests)`` keeps the public entry point.  It works in two
phases:

1. Static validation of the whole request envelope.  Any shape problem
   (duplicate ids, non-contiguous groups, unknown ops, forward/unknown
   dependencies, references missing from ``depends``, ...) raises
   ``StoreError('BAD_BATCH')`` before a single database statement is issued.
2. Group-by-group execution.  Each group runs in one store transaction, so
   its prefix is visible only to later requests *of the same group*.  The
   batch keeps its own snapshot map of per-request results; cross-group
   references can therefore only see results of already committed groups, and
   a rolled-back group's results disappear from the map.

The store joins the batch's transaction via its ``txn=`` parameter and never
commits it itself; the group transaction ends with exactly one COMMIT or
ROLLBACK, so the connection is always usable again afterwards.
"""
from store import StoreError

KNOWN_OPS = frozenset(('create', 'read', 'update', 'delete'))
MAX_GROUPS = 10
MAX_REQUESTS = 40


def _is_ref(node):
    return (isinstance(node, dict) and len(node) == 2
            and '$result' in node and 'field' in node
            and isinstance(node.get('field'), str))


def _check_ref(node, self_id, ids, declared):
    """Validate one literal-or-reference node used by key/value/if_match.

    Any dict carrying ``$result`` must have the full reference shape; plain
    literals are left for runtime type checks at the store layer.
    """
    if not isinstance(node, dict) or '$result' not in node:
        return
    if not _is_ref(node):
        raise StoreError('BAD_BATCH')
    ref_id = node['$result']
    if not isinstance(ref_id, str) or ref_id not in ids:
        raise StoreError('BAD_BATCH')
    if ref_id not in declared:
        # Every referenced id must be explicitly declared in depends.
        raise StoreError('BAD_BATCH')
    if ref_id == self_id:
        raise StoreError('BAD_BATCH')


def _preflight(requests):
    if not isinstance(requests, list):
        raise StoreError('BAD_BATCH')
    if len(requests) > MAX_REQUESTS:
        raise StoreError('BAD_BATCH')

    ids = set()
    groups = []          # ordered, de-duplicated group names
    seen_groups = set()
    last_group = object()

    for i, req in enumerate(requests):
        if not isinstance(req, dict):
            raise StoreError('BAD_BATCH')
        for field in ('id', 'group', 'depends', 'op', 'key'):
            if field not in req:
                raise StoreError('BAD_BATCH')
        rid, group, depends, op, key = (
            req['id'], req['group'], req['depends'], req['op'], req['key'])
        if not isinstance(rid, str) or not rid or rid in ids:
            raise StoreError('BAD_BATCH')  # missing or duplicate id
        ids.add(rid)
        if not isinstance(group, str):
            raise StoreError('BAD_BATCH')
        if not isinstance(depends, list) or not all(
                isinstance(d, str) for d in depends):
            raise StoreError('BAD_BATCH')
        if op not in KNOWN_OPS:
            raise StoreError('BAD_BATCH')
        if group != last_group:
            if group in seen_groups:
                raise StoreError('BAD_BATCH')  # group not contiguous
            seen_groups.add(group)
            groups.append(group)
            last_group = group
        if len(groups) > MAX_GROUPS:
            raise StoreError('BAD_BATCH')

        # body shape per op; value (literal or reference) is runtime-checked.
        body = req.get('body')
        if op in ('create', 'update'):
            if not isinstance(body, dict) or 'value' not in body:
                raise StoreError('BAD_BATCH')
        elif body is not None:
            raise StoreError('BAD_BATCH')
        if op in ('update', 'delete') and 'if_match' not in req:
            raise StoreError('BAD_BATCH')

        # Dependency declarations: known ids, earlier only, no self.
        declared = set()
        for dep in depends:
            if dep == rid or dep not in ids:
                # forward/unknown dependency, or self reference
                raise StoreError('BAD_BATCH')
            declared.add(dep)

        _check_ref(key, rid, ids, declared)
        if body is not None:
            _check_ref(body['value'], rid, ids, declared)
        if 'if_match' in req:
            _check_ref(req['if_match'], rid, ids, declared)

    return groups


def _resolve(node, available):
    """Resolve a literal-or-reference node against published/same-group
    result snapshots.  Runtime failures raise REFERENCE_FAILED."""
    if not _is_ref(node):
        return node
    ref_id, field = node['$result'], node['field']
    result = available.get(ref_id)
    if result is None:
        # Rolled-back / not-executed / failed-dependency result, or an id
        # whose group was rejected: no NULL guessing, no version guessing.
        raise StoreError('REFERENCE_FAILED')
    if field not in result:
        # e.g. value/etag on a delete result, deleted on a row result.
        raise StoreError('REFERENCE_FAILED')
    return result[field]


def run(store, requests):
    groups = _preflight(requests)
    responses = [None] * len(requests)
    # Request index per id, and member indices per group, in array order.
    index_of = {req['id']: i for i, req in enumerate(requests)}
    members = {group: [] for group in groups}
    for i, req in enumerate(requests):
        members[req['group']].append(i)

    # Results available for references: committed results of earlier groups
    # plus tentative results of the currently running group.  Entries are
    # removed as soon as their group fails.
    available = {}

    for group in groups:
        idxs = members[group]

        # Failed-dependency gate, evaluated against *cross-group* dependencies
        # only: same-group members are always available while the group runs
        # (and are handled by the rollback logic below).
        blocked = any(
            requests[index_of[dep]]['group'] != group
            and dep not in available
            for i in idxs
            for dep in requests[i]['depends']
        )
        if blocked:
            for i in idxs:
                responses[i] = {'id': requests[i]['id'],
                                'status': 'failed_dependency'}
            continue

        tentative = {}
        failure = None
        failure_index = None
        txn = store.begin()
        try:
            for position, i in enumerate(idxs):
                req = requests[i]
                try:
                    key = _resolve(req['key'], available)
                    op = req['op']
                    if op == 'create':
                        result = store.create(
                            key, _resolve(req['body']['value'], available),
                            txn=txn)
                    elif op == 'read':
                        result = store.read(key, txn=txn)
                    elif op == 'update':
                        result = store.update(
                            key,
                            _resolve(req['body']['value'], available),
                            _resolve(req['if_match'], available),
                            txn=txn)
                    else:  # delete
                        result = store.delete(
                            key,
                            _resolve(req['if_match'], available),
                            txn=txn)
                except StoreError as exc:
                    failure = exc.code
                    failure_index = position
                    responses[i] = {'id': req['id'], 'status': 'failed',
                                    'error': failure}
                    break

                tentative[req['id']] = result
                available[req['id']] = result
                responses[i] = {'id': req['id'], 'status': 'ok',
                                'result': result}
        except BaseException:
            # Unexpected driver-level error: still end the transaction and
            # mark the rest of the group so the connection stays usable.
            txn.rollback()
            for rid in tentative:
                available.pop(rid, None)
            for j in range(len(tentative), len(idxs)):
                responses[idxs[j]] = {'id': requests[idxs[j]]['id'],
                                      'status': 'not_executed'}
            for position in range(len(tentative)):
                rid = requests[idxs[position]]['id']
                responses[idxs[position]] = {
                    'id': rid, 'status': 'rolled_back',
                    'result': tentative[rid]}
            raise

        if failure is None:
            txn.commit()
        else:
            txn.rollback()
            # The group's prefix never committed; remove its results so later
            # groups cannot reference uncommitted or rolled-back data.
            for rid in tentative:
                available.pop(rid, None)
            for position in range(failure_index):
                rid = requests[idxs[position]]['id']
                responses[idxs[position]] = {
                    'id': rid, 'status': 'rolled_back',
                    'result': tentative[rid]}
            for position in range(failure_index + 1, len(idxs)):
                responses[idxs[position]] = {
                    'id': requests[idxs[position]]['id'],
                    'status': 'not_executed'}

    return responses
