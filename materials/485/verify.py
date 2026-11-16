"""Real-SQLite verification of the conditional store and batch orchestration.

Run from this directory:

    python3 verify.py

Everything runs against actual SQLite database files (temporary directory),
including two independent connections/threads contending on one etag.
Exits non-zero on the first failed check.
"""
import copy
import random
import tempfile
import threading
from pathlib import Path

from store import Store, StoreError, MAX_VALUE
import batch

CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError(label)
    print('  ok -', label)


def expect_error(fn, code, label):
    try:
        fn()
    except StoreError as exc:
        check(exc.code == code, '%s -> %s' % (label, code))
        return
    raise AssertionError('%s did not fail (expected %s)' % (label, code))


def section(title):
    print('\n== %s ==' % title)


# ----------------------------------------------------------------- model mirror

class ModelTxn:
    """Snapshot/restore transaction for the in-memory reference backend."""

    def __init__(self, model):
        self.model = model
        self.snapshot = (copy.deepcopy(model.state), model.next_ver)

    def commit(self):
        self.snapshot = None

    def rollback(self):
        if self.snapshot is not None:
            self.model.state, self.model.next_ver = self.snapshot
            self.snapshot = None


class ModelStore:
    """Reference semantics in plain dicts; same method surface as Store."""

    def __init__(self):
        self.state = {}       # key -> [value, ver]
        self.next_ver = 1

    def begin(self):
        return ModelTxn(self)

    def create(self, key, value, txn=None):
        if key in self.state:
            raise StoreError('CONSTRAINT')
        ver = self.next_ver
        self.next_ver += 1
        self.state[key] = [value, ver]
        return {'key': key, 'value': value, 'etag': 'm-%d' % ver}

    def read(self, key, txn=None):
        if key not in self.state:
            raise StoreError('NOT_FOUND')
        value, ver = self.state[key]
        return {'key': key, 'value': value, 'etag': 'm-%d' % ver}

    def _match(self, key, if_match):
        if if_match is None:
            raise StoreError('PRECONDITION_REQUIRED')
        if not isinstance(if_match, str):
            raise StoreError('BAD_REQUEST')
        if key not in self.state:
            raise StoreError('NOT_FOUND')
        if self.state[key][1] != int(if_match.split('-')[1]):
            raise StoreError('PRECONDITION_FAILED')

    def update(self, key, value, if_match, txn=None):
        self._match(key, if_match)
        ver = self.next_ver
        self.next_ver += 1
        self.state[key] = [value, ver]
        return {'key': key, 'value': value, 'etag': 'm-%d' % ver}

    def delete(self, key, if_match, txn=None):
        self._match(key, if_match)
        del self.state[key]
        return {'key': key, 'deleted': True}


# ----------------------------------------------------------------- random gen

def generate_requests(rng):
    """Generate a valid envelope within 10 groups / 40 requests."""
    group_count = rng.randint(1, 10)
    total = rng.randint(group_count, 40)
    sizes = [1] * group_count
    for _ in range(total - group_count):
        sizes[rng.randrange(group_count)] += 1

    requests, ids = [], []
    for gi, size in enumerate(sizes):
        group_start = len(ids)
        for _ in range(size):
            rid = 'r%d' % len(requests)
            ids.append(rid)
            earlier = list(ids[:-1])
            depends = []

            def ref(target_id, field):
                if target_id not in depends:
                    depends.append(target_id)
                return {'$result': target_id, 'field': field}

            op = rng.choice(['create', 'create', 'read', 'read',
                             'update', 'delete'])
            req = {'id': rid, 'group': 'G%d' % gi, 'op': op}

            if op == 'create':
                key = 'K%d' % rng.randrange(8)
                if earlier and rng.random() < 0.5:
                    src = rng.choice(earlier)
                    value = ref(src, 'value') if rng.random() < 0.6 else \
                        rng.randint(0, MAX_VALUE)
                    if rng.random() < 0.1:
                        value = ref(src, 'nope')  # missing field
                else:
                    value = rng.randint(0, MAX_VALUE)
                req['key'] = key
                req['body'] = {'value': value}
            elif op == 'read':
                if earlier and rng.random() < 0.5:
                    req['key'] = ref(rng.choice(earlier), 'key')
                else:
                    req['key'] = 'K%d' % rng.randrange(8)
            else:
                if earlier and rng.random() < 0.8:
                    src = rng.choice(earlier)
                    req['key'] = ref(src, 'key')
                    req['if_match'] = ref(src, 'etag')
                else:
                    req['key'] = 'K%d' % rng.randrange(8)
                    req['if_match'] = 'm-0'  # always stale/foreign
                if op == 'update':
                    if earlier and rng.random() < 0.4:
                        value = ref(rng.choice(earlier), 'value')
                    else:
                        value = rng.randint(0, MAX_VALUE)
                    req['body'] = {'value': value}
                if rng.random() < 0.1 and earlier:
                    req['if_match'] = ref(rng.choice(earlier), 'deleted')

            if rng.random() < 0.2 and earlier:
                extra = rng.choice(earlier)
                if extra not in depends:
                    depends.append(extra)
            req['depends'] = depends
            requests.append(req)
    return requests


def compare_responses(real, model, label):
    check(len(real) == len(model), '%s: response count' % label)
    for a, b in zip(real, model):
        check(a['id'] == b['id'] and a['status'] == b['status'],
              '%s: %s status %s == %s' % (label, a['id'], a['status'], b['status']))
        if a['status'] == 'failed':
            check(a['error'] == b['error'],
                  '%s: %s error %s == %s' % (label, a['id'], a['error'], b['error']))
        elif a['status'] == 'ok':
            check(set(a['result']) == set(b['result'])
                  and all(a['result'].get(k) == b['result'].get(k)
                          for k in ('key', 'value', 'deleted')),
                  '%s: %s result payload (%r vs %r)'
                  % (label, a['id'], a['result'], b['result']))


# =================================================================
# Tests
# =================================================================

def main():
    with tempfile.TemporaryDirectory(prefix='verify-485-') as folder:
        db_path = Path(folder) / 'work.db'

        section('1. direct CRUD, error codes and input rules')
        store = Store(db_path)
        created = store.create('alpha', 10)
        check(created == {'key': 'alpha', 'value': 10, 'etag': created['etag']}
              and isinstance(created['etag'], str), 'create returns row + opaque etag')
        expect_error(lambda: store.create('alpha', 1), 'CONSTRAINT',
                     'create on existing key')
        check(store.read('alpha')['value'] == 10, 'read returns the row')
        expect_error(lambda: store.read('missing'), 'NOT_FOUND', 'read missing')
        expect_error(lambda: store.update('alpha', 11, None),
                     'PRECONDITION_REQUIRED', 'update without if_match')
        expect_error(lambda: store.delete('alpha', None),
                     'PRECONDITION_REQUIRED', 'delete without if_match')
        expect_error(lambda: store.update('ghost', 1, 'W/"x-1"'), 'NOT_FOUND',
                     'update missing key')
        expect_error(lambda: store.delete('ghost', 'W/"x-1"'), 'NOT_FOUND',
                     'delete missing key')
        expect_error(lambda: store.update('alpha', 11, 'W/"deadbeef-999999"'),
                     'PRECONDITION_FAILED', 'update with foreign etag')
        expect_error(lambda: store.delete('alpha', 'W/"deadbeef-999999"'),
                     'PRECONDITION_FAILED', 'delete with foreign etag')
        stale = created['etag']
        updated = store.update('alpha', 10, stale)  # same value, still a new version
        check(updated['etag'] != stale, 'same-value update still changes etag')
        expect_error(lambda: store.update('alpha', 12, stale),
                     'PRECONDITION_FAILED', 'old etag invalid after update')
        expect_error(lambda: store.create('b', True), 'BAD_REQUEST',
                     'bool is not an int value')
        expect_error(lambda: store.create('f', 1.5), 'BAD_REQUEST',
                     'float rejected')
        expect_error(lambda: store.create('n', -1), 'BAD_REQUEST',
                     'negative value rejected')
        expect_error(lambda: store.create('n', MAX_VALUE + 1), 'BAD_REQUEST',
                     'value above int64 rejected')
        edge = store.create('edge', MAX_VALUE)
        check(edge['value'] == MAX_VALUE, 'max int64 accepted')
        expect_error(lambda: store.create('', 1), 'BAD_REQUEST', 'empty key')
        expect_error(lambda: store.create('字' * 65, 1), 'BAD_REQUEST',
                     'key longer than 64 code points')
        check(store.create('字' * 64, 1)['key'] == '字' * 64,
              '64 code-point key accepted (code points, not bytes)')
        expect_error(lambda: store.create(7, 1), 'BAD_REQUEST', 'non-text key')
        expect_error(lambda: store.update('alpha', 1, True), 'BAD_REQUEST',
                     'bool if_match rejected')
        # callers cannot pin internal revision/lifetime fields: the API exposes no
        # such parameter, the ver column is entirely service-managed.
        row = store.db.execute('SELECT ver FROM records WHERE key=?',
                               ('edge',)).fetchone()
        check(isinstance(row[0], int) and row[0] >= 1,
              'revision/version identity is service-generated, not caller-supplied')

        section('2. delete/re-create cannot revive an etag, including after reopen')
        etag1 = store.create('cycle', 5)['etag']
        store.delete('cycle', etag1)
        expect_error(lambda: store.read('cycle'), 'NOT_FOUND', 'row gone after delete')
        etag2 = store.create('cycle', 5)['etag']  # same key, same value
        check(etag1 != etag2, 're-created row gets a new etag')
        expect_error(lambda: store.update('cycle', 6, etag1),
                     'PRECONDITION_FAILED', 'pre-delete etag cannot update')
        expect_error(lambda: store.delete('cycle', etag1),
                     'PRECONDITION_FAILED', 'pre-delete etag cannot delete')
        store.close()
        reopened = Store(db_path)
        check(reopened.update('cycle', 7, etag2)['value'] == 7,
              'current etag survives close/reopen')
        expect_error(lambda: reopened.update('cycle', 8, etag1),
                     'PRECONDITION_FAILED', 'old etag stays dead after reopen')
        etag3 = reopened.read('cycle')['etag']
        reopened.delete('cycle', etag3)
        reopened.create('cycle', 9)
        expect_error(lambda: reopened.update('cycle', 1, etag3),
                     'PRECONDITION_FAILED', 'etag of previous lifetime stays dead')
        store = reopened

        section('3. rollback never invalidates a committed etag')
        e = store.create('rb', 1)['etag']
        e2 = store.update('rb', 2, e)['etag']
        txn = store.begin()
        expect_error(lambda: store.update('rb', 3, 'W/"deadbeef-1"', txn=txn),
                     'PRECONDITION_FAILED', 'failing op inside caller txn')
        txn.rollback()
        current = store.read('rb')['etag']
        check(current == e2, 'failed rollback changed nothing')
        check(store.update('rb', 4, e2)['value'] == 4,
              'committed etag still valid after rolled-back failure')
        e4 = store.read('rb')['etag']
        txn = store.begin()
        store.update('rb', 5, e4, txn=txn)
        txn.rollback()  # the new version was never committed
        check(store.read('rb')['etag'] == e4,
              'rolled-back write leaves the committed version in place')
        check(store.update('rb', 7, e4)['value'] == 7,
              'committed etag survives a rolled-back successful-looking write')

        section('4. uncommitted group prefix is invisible to another connection')
        other = Store(db_path)
        txn = store.begin()
        store.create('secret', 1, txn=txn)
        expect_error(lambda: other.read('secret'), 'NOT_FOUND',
                     'second connection cannot see uncommitted prefix')
        txn.rollback()
        expect_error(lambda: other.read('secret'), 'NOT_FOUND',
                     'still invisible after rollback')
        other.close()

        section('5. references read result snapshots, not the current row')
        snap_requests = [
            {'id': 'c1', 'group': 'G', 'depends': [], 'op': 'create',
             'key': 'snap', 'body': {'value': 1}},
            {'id': 'u1', 'group': 'H', 'depends': ['c1'], 'op': 'update',
             'key': {'$result': 'c1', 'field': 'key'},
             'if_match': {'$result': 'c1', 'field': 'etag'},
             'body': {'value': 2}},
            {'id': 'copy', 'group': 'I', 'depends': ['c1'], 'op': 'create',
             'key': 'snap_copy',
             'body': {'value': {'$result': 'c1', 'field': 'value'}}},
            {'id': 'inside', 'group': 'J', 'depends': ['c1', 'u1'], 'op': 'create',
             'key': 'snap_inside',
             'body': {'value': {'$result': 'u1', 'field': 'value'}}},
        ]
        res = batch.run(store, snap_requests)
        check(all(r['status'] == 'ok' for r in res), 'snapshot batch all ok')
        check(store.read('snap')['value'] == 2, 'row was updated to 2')
        check(store.read('snap_copy')['value'] == 1,
              'cross-group reference keeps the committed snapshot value 1')
        check(store.read('snap_inside')['value'] == 2,
              'reference follows the referenced result snapshot')
        # same-group snapshot: later writes do not mutate an earlier result
        same_group = [
            {'id': 'g_c', 'group': 'G', 'depends': [], 'op': 'create',
             'key': 'sg', 'body': {'value': 1}},
            {'id': 'g_u', 'group': 'G', 'depends': ['g_c'], 'op': 'update',
             'key': {'$result': 'g_c', 'field': 'key'},
             'if_match': {'$result': 'g_c', 'field': 'etag'},
             'body': {'value': 2}},
            {'id': 'g_copy', 'group': 'H', 'depends': ['g_c'], 'op': 'create',
             'key': 'sg_copy',
             'body': {'value': {'$result': 'g_c', 'field': 'value'}}},
        ]
        batch.run(store, same_group)
        check(store.read('sg_copy')['value'] == 1,
              "g_c's snapshot stays 1 after the same-group update to 2")
        ref_fail = batch.run(store, [
            {'id': 'd', 'group': 'G', 'depends': [], 'op': 'delete',
             'key': 'sg', 'if_match': store.read('sg')['etag']},
            {'id': 'badref', 'group': 'H', 'depends': ['d'], 'op': 'create',
             'key': 'x',
             'body': {'value': {'$result': 'd', 'field': 'etag'}}}])
        check(ref_fail[1]['status'] == 'failed'
              and ref_fail[1]['error'] == 'REFERENCE_FAILED',
              "referencing etag on a delete result is a failed reference, no NULL guess")
        expect_error(lambda: store.read('sg'), 'NOT_FOUND',
                     'the delete group committed; only the bad-reference group failed')
        check(store.read('sg_copy')['value'] == 1,
              'unrelated committed rows remain readable')

        section('6. failed group: rollback, rolled_back/not_executed, dependency gate')
        failed_req = [
            {'id': 'make', 'group': 'G', 'depends': [], 'op': 'create',
             'key': 'A', 'body': {'value': 10}},
            {'id': 'bad', 'group': 'G', 'depends': ['make'], 'op': 'update',
             'key': {'$result': 'make', 'field': 'key'},
             'if_match': {'$result': 'make', 'field': 'etag'},
             'body': {'value': -1}},
            {'id': 'after', 'group': 'G', 'depends': ['make'], 'op': 'read',
             'key': {'$result': 'make', 'field': 'key'}},
            {'id': 'dependent', 'group': 'H', 'depends': ['make'], 'op': 'read',
             'key': {'$result': 'make', 'field': 'key'}},
            {'id': 'independent', 'group': 'I', 'depends': [], 'op': 'create',
             'key': 'Z', 'body': {'value': 30}},
        ]
        res = batch.run(store, failed_req)
        statuses = {r['id']: r['status'] for r in res}
        check(statuses == {'make': 'rolled_back', 'bad': 'failed',
                           'after': 'not_executed', 'dependent': 'failed_dependency',
                           'independent': 'ok'},
              'group failure statuses')
        check(next(r for r in res if r['id'] == 'bad')['error'] == 'BAD_REQUEST',
              'fault item keeps its concrete error')
        check(next(r for r in res if r['id'] == 'make')['result']['key'] == 'A',
              'rolled_back item still carries the tentative result')
        rows = dict((k, v) for k, v, _ in
                    store.db.execute('SELECT key, value, ver FROM records'))
        check('A' not in rows, 'rolled-back A is absent from the real database')
        check(rows.get('Z') == 30, 'independent group committed Z')
        check(store.read('Z')['value'] == 30,
              'connection still serves independent reads after the failed batch')

        section('7. cross-group failed reference and failed-group chaining')
        chain = [
            {'id': 'a', 'group': 'G', 'depends': [], 'op': 'create',
             'key': 'chainA', 'body': {'value': 1}},
            {'id': 'b', 'group': 'G', 'depends': ['a'], 'op': 'create',
             'key': 'chainA', 'body': {'value': 2}},  # CONSTRAINT -> group rollback
            {'id': 'dep1', 'group': 'H', 'depends': ['a'], 'op': 'read',
             'key': {'$result': 'a', 'field': 'key'}},
            {'id': 'dep2', 'group': 'J', 'depends': ['dep1'], 'op': 'read',
             'key': {'$result': 'dep1', 'field': 'key'}},
            {'id': 'z', 'group': 'K', 'depends': [], 'op': 'create',
             'key': 'chainZ', 'body': {'value': 9}},
        ]
        res = batch.run(store, chain)
        statuses = {r['id']: r['status'] for r in res}
        check(statuses == {'a': 'rolled_back', 'b': 'failed',
                           'dep1': 'failed_dependency', 'dep2': 'failed_dependency',
                           'z': 'ok'},
              'transitive failed dependency and independent continuation')
        check(store.read('chainZ')['value'] == 9, 'later independent group commits')

        section('8. BAD_BATCH envelope rejection performs no writes')
        before = list(store.db.execute('SELECT key FROM records'))
        bad_batches = [
            [{'id': 'x', 'group': 'G', 'depends': [], 'op': 'create',
              'key': 'k', 'body': {'value': 1}},
             {'id': 'x', 'group': 'H', 'depends': [], 'op': 'create',
              'key': 'k2', 'body': {'value': 1}}],                      # duplicate id
            [{'id': 'x', 'group': 'G', 'depends': [], 'op': 'create',
              'key': 'k', 'body': {'value': 1}},
             {'id': 'y', 'group': 'H', 'depends': [], 'op': 'create',
              'key': 'k2', 'body': {'value': 1}},
             {'id': 'z', 'group': 'G', 'depends': [], 'op': 'create',
              'key': 'k3', 'body': {'value': 1}}],                     # non-contiguous
            [{'id': 'x', 'group': 'G', 'depends': [], 'op': 'explode',
              'key': 'k'}],                                            # unknown op
            [{'id': 'x', 'group': 'G', 'depends': ['y'], 'op': 'read',
              'key': 'k'},
             {'id': 'y', 'group': 'G', 'depends': [], 'op': 'create',
              'key': 'k2', 'body': {'value': 1}}],                     # forward dep
            [{'id': 'x', 'group': 'G', 'depends': ['ghost'], 'op': 'read',
              'key': 'k'}],                                            # unknown dep
            [{'id': 'x', 'group': 'G', 'depends': [], 'op': 'read',
              'key': {'$result': 'y', 'field': 'key'}},
             {'id': 'y', 'group': 'G', 'depends': [], 'op': 'create',
              'key': 'k2', 'body': {'value': 1}}],                     # ref not declared
            [{'id': 'x', 'group': 'G', 'depends': ['x'], 'op': 'read',
              'key': 'k'}],                                            # self dependency
            [{'id': 'x', 'group': 'G', 'depends': [], 'op': 'create',
              'key': 'k'}],                                            # missing body
            [{'id': 'x', 'group': 'G', 'depends': [], 'op': 'update',
              'key': 'k', 'body': {'value': 1}}],                      # missing if_match
            [{'id': 'x', 'group': 'G', 'depends': [], 'op': 'read',
              'key': {'$result': 'x'}}],                               # malformed ref
        ]
        for bad in bad_batches:
            expect_error(lambda b=bad: batch.run(store, b), 'BAD_BATCH',
                         'bad envelope rejected')
        check(list(store.db.execute('SELECT key FROM records')) == before,
              'no rows changed by BAD_BATCH requests')
        big_groups = [{'id': 'g%d' % i, 'group': 'G%d' % i, 'depends': [],
                       'op': 'create', 'key': 'lim%d' % i,
                       'body': {'value': 1}} for i in range(11)]
        expect_error(lambda: batch.run(store, big_groups), 'BAD_BATCH',
                     '11 groups rejected')
        big_req = [{'id': 'r%d' % i, 'group': 'G', 'depends': [],
                    'op': 'create', 'key': 'lim%d' % i,
                    'body': {'value': 1}} for i in range(41)]
        expect_error(lambda: batch.run(store, big_req), 'BAD_BATCH',
                     '41 requests rejected')

        section('9. two+ real connections contending on one etag: at most one wins')
        concurrent_path = Path(folder) / 'concurrent.db'
        boot = Store(concurrent_path)
        shared_etag = boot.create('hot', 0)['etag']
        boot.close()
        # Eight independent connections to the same database file: the same
        # situation as two direct clients, scaled up to stress the decision.
        clients = [Store(concurrent_path) for _ in range(8)]
        outcomes = []
        lock = threading.Lock()

        def worker(conn, value, kind):
            try:
                if kind == 'update':
                    result = conn.update('hot', value, shared_etag)
                else:
                    result = conn.delete('hot', shared_etag)
                with lock:
                    outcomes.append(('ok', value, result))
            except StoreError as exc:
                with lock:
                    outcomes.append((exc.code, value, None))

        threads = []
        for i in range(8):
            threads.append(threading.Thread(
                target=worker, args=(clients[i], i + 1,
                                     'update' if i < 6 else 'delete')))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wins = [o for o in outcomes if o[0] == 'ok']
        losses = [o for o in outcomes if o[0] != 'ok']
        check(len(wins) == 1, 'exactly one conditional write succeeds')
        check(all(code in ('PRECONDITION_FAILED', 'NOT_FOUND')
                  for code, _, _ in losses), 'losers get condition/not-found errors')
        winner_value, winner_result = wins[0][1], wins[0][2]
        probe = Store(concurrent_path)
        if 'etag' in winner_result:
            check(winner_result['value'] == winner_value,
                  "winner's returned row is its own write, not swapped")
            check(probe.read('hot')['value'] == winner_value,
                  'database matches the winning write')
        else:
            expect_error(lambda: probe.read('hot'), 'NOT_FOUND',
                         'winner deleted the row')
        # every losing connection is still usable for independent work
        for conn in clients:
            check(conn.db.in_transaction is False,
                  'no connection left inside an open transaction')
        fresh = probe.read('hot')['etag'] if wins[0][2].get('etag') else None
        if fresh is not None:
            check(clients[0].update('hot', 77, fresh)['value'] == 77,
                  'losing connection recovered with a fresh etag')
        for conn in clients:
            conn.close()
        probe.close()

        # strict two-client interleaving: both hold the same etag, first commits,
        # the second must lose on the atomic compare-and-write.
        a = Store(concurrent_path)
        b = Store(concurrent_path)
        et = a.read('hot')['etag']
        check(a.update('hot', 101, et)['value'] == 101, 'client A wins first write')
        expect_error(lambda: b.update('hot', 202, et), 'PRECONDITION_FAILED',
                     'client B loses the same-etag race atomically')
        expect_error(lambda: b.delete('hot', et), 'PRECONDITION_FAILED',
                     'client B cannot delete with the stale etag either')
        check(a.read('hot')['value'] == 101, "A's result was not swapped by B")
        a.close()
        b.close()

        section('10. legal-range batch parity against an in-memory reference backend')
        for seed in range(60):
            rng = random.Random(seed)
            requests = generate_requests(rng)
            check(len(requests) <= 40 and
                  len({r['group'] for r in requests}) <= 10,
                  'seed %d stays inside 10 groups / 40 requests' % seed)
            real_store = Store(Path(folder) / ('rand%d.db' % seed))
            model_store = ModelStore()
            real_res = batch.run(real_store, copy.deepcopy(requests))
            model_res = batch.run(model_store, copy.deepcopy(requests))
            compare_responses(real_res, model_res, 'seed %d' % seed)
            real_rows = dict((k, v) for k, v in
                             real_store.db.execute('SELECT key, value FROM records'))
            model_rows = dict((k, vv[0]) for k, vv in model_store.state.items())
            check(real_rows == model_rows,
                  'seed %d: final database rows match the reference model' % seed)
            real_store.close()

        section('11. 100-row scale and maximum-shaped legal batch')
        scale_store = Store(Path(folder) / 'scale.db')
        for chunk in range(3):
            reqs = [{'id': 'r%d' % n, 'group': 'bulk',
                     'depends': [], 'op': 'create', 'key': 'row%03d' % n,
                     'body': {'value': n}}
                    for n in range(chunk * 34, min(100, (chunk + 1) * 34))]
            # distinct group per batch keeps each envelope under the limits
            for r in reqs:
                r['group'] = 'bulk%d' % chunk
            res = batch.run(scale_store, reqs)
            check(all(x['status'] == 'ok' for x in res), 'scale chunk %d ok' % chunk)
        check(scale_store.db.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 100,
              '100 rows live in the real database')
        ten_groups = [{'id': 't%d' % i, 'group': 'G%d' % i, 'depends': [],
                       'op': 'create', 'key': 'ten%d' % i,
                       'body': {'value': i}} for i in range(10)]
        # pad to 40 requests with same-group reads
        for i in range(30):
            ten_groups.append({'id': 't10_%d' % i, 'group': 'G%d' % (i % 10),
                               'depends': [], 'op': 'read',
                               'key': 'ten%d' % (i % 10)})
        ten_groups.sort(key=lambda r: r['group'])  # restore contiguous groups
        res = batch.run(scale_store, ten_groups)
        check(len(res) == 40 and all(r['status'] == 'ok' for r in res),
              '10-group / 40-request legal batch succeeds')
        scale_store.close()
        store.close()

        print('\nALL %d CHECKS PASSED' % CHECKS)


if __name__ == '__main__':
    main()
