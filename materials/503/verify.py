"""Independent verification for the 503 rights/warehouse SDK.

Run with:  python3 materials/503/verify.py

Nothing here imports the production balance output as the sole judgement: the
expected values are derived from the initial grants, the raw persisted
messages and the successful row operations.  The LocalQuotaFixture is only an
isolated test facility -- it is not a production module and has no cross-site
transfer behaviour.
"""
import json
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from rights import Rights, canonical  # noqa: E402
from warehouse_rows import WarehouseRows  # noqa: E402
from transport import export_message, read_message  # noqa: E402
import audit  # noqa: E402


CHECKS = []
FAILURES = []


def check(name):
    def decorator(fn):
        CHECKS.append((name, fn))
        return fn
    return decorator


def expect_error(name, fn, exc=ValueError):
    try:
        fn()
    except exc:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f'{name}: wrong exception {type(exc).__name__}: {exc}')
    raise AssertionError(f'{name}: expected {exc.__name__}')


# ---------------------------------------------------------------------------
# Isolated test facility: an accurate local quota participant with the same
# transaction contract (spend/restore inside the caller's transaction).
# It is not a production module and implements no transfers.
# ---------------------------------------------------------------------------
class LocalQuotaFixture:
    def __init__(self, db, initial_grant):
        self.db = db
        self.initial_grant = initial_grant
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS fixture_quota(
                k TEXT PRIMARY KEY, available INTEGER NOT NULL,
                initial INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS fixture_usages(
                id TEXT PRIMARY KEY, units INTEGER NOT NULL,
                returned INTEGER NOT NULL);
        ''')
        self.db.execute(
            'INSERT OR IGNORE INTO fixture_quota VALUES(?,?,?)',
            ('q', initial_grant, initial_grant),
        )

    def _ensure_txn(self):
        if not self.db.in_transaction:
            self.db.execute('BEGIN IMMEDIATE')

    def spend(self, usage, units=1):
        if type(units) is not int or units <= 0:
            raise ValueError('invalid consumption')
        self._ensure_txn()
        old = self.db.execute(
            'SELECT * FROM fixture_usages WHERE id=?', (usage,)
        ).fetchone()
        if old is not None:
            if old['units'] != units or old['returned']:
                raise ValueError('usage identity conflict')
            return
        avail = self.db.execute(
            'SELECT available FROM fixture_quota WHERE k=?', ('q',)
        ).fetchone()[0]
        if units > avail:
            raise ValueError('insufficient local rights')
        self.db.execute(
            'UPDATE fixture_quota SET available=available-? WHERE k=?',
            (units, 'q'),
        )
        self.db.execute(
            'INSERT INTO fixture_usages VALUES(?,?,0)', (usage, units)
        )

    def restore(self, usage):
        self._ensure_txn()
        row = self.db.execute(
            'SELECT * FROM fixture_usages WHERE id=?', (usage,)
        ).fetchone()
        if row is None:
            raise ValueError('unknown usage')
        if not row['returned']:
            self.db.execute(
                'UPDATE fixture_quota SET available=available+? WHERE k=?',
                (row['units'], 'q'),
            )
            self.db.execute(
                'UPDATE fixture_usages SET returned=1 WHERE id=?', (usage,)
            )

    def available(self):
        return self.db.execute(
            'SELECT available FROM fixture_quota WHERE k=?', ('q',)
        ).fetchone()[0]


def tmpdir():
    return tempfile.mkdtemp(prefix='rights503-')


# ---------------------------------------------------------------------------
# 1. SDK-only behaviour
# ---------------------------------------------------------------------------
@check('sdk spend/restore, duplicate restore, conflicts')
def _():
    d = tmpdir()
    r = Rights(os.path.join(d, 'a.db'), 'A', 5)
    assert r.available() == 5
    r.spend('u1', 2)
    assert r.available() == 3
    r.spend('u1', 2)                    # identical retry: no double spend
    assert r.available() == 3
    expect_error('same usage different units', lambda: r.spend('u1', 1))
    r.restore('u1')
    assert r.available() == 5
    r.restore('u1')                     # duplicate restore: no effect
    assert r.available() == 5
    expect_error('spend returned usage', lambda: r.spend('u1', 2))
    expect_error('unknown restore', lambda: r.restore('nope'))
    expect_error('zero units', lambda: r.spend('u2', 0))
    expect_error('bool units', lambda: r.spend('u2', True))
    expect_error('overdraft', lambda: r.spend('u3', 6))
    assert r.available() == 5
    r.close()


@check('transfer retry, conflict, self, invalid destination')
def _():
    d = tmpdir()
    a = Rights(os.path.join(d, 'a.db'), 'A', 5)
    b = Rights(os.path.join(d, 'b.db'), 'B', 2)
    m = a.transfer('t1', 'B', 3)
    assert m == {'id': 'A:t1', 'source': 'A', 'destination': 'B', 'units': 3}
    assert a.available() == 2
    again = a.transfer('t1', 'B', 3)
    assert again == m and a.available() == 2
    expect_error('same id different units',
                 lambda: a.transfer('t1', 'B', 2))
    expect_error('same id different destination',
                 lambda: a.transfer('t1', 'A', 3))
    expect_error('transfer to self', lambda: a.transfer('t2', 'A', 1))
    expect_error('transfer to C', lambda: a.transfer('t2', 'C', 1))
    expect_error('transfer zero', lambda: a.transfer('t3', 'B', 0))
    expect_error('transfer over balance', lambda: a.transfer('t4', 'B', 99))
    assert a.available() == 2
    assert b.available() == 2              # not received yet: still in flight
    a.close()
    b.close()


@check('receive dedup by canonical content; reorder and repeat')
def _():
    d = tmpdir()
    a = Rights(os.path.join(d, 'a.db'), 'A', 6)
    b = Rights(os.path.join(d, 'b.db'), 'B', 0)
    m1 = a.transfer('t1', 'B', 2)
    m2 = a.transfer('t2', 'B', 3)
    assert a.available() == 1
    # Reverse order, then repeat, and reordered JSON keys.
    b.receive({k: m2[k] for k in reversed(list(m2))})
    b.receive(m1)
    b.receive(m1)
    b.receive(m2)
    assert b.available() == 5
    # Same id, different content must be rejected with no state change.
    bad = dict(m1, units=1)
    expect_error('same id different units at receive', lambda: b.receive(bad))
    assert b.available() == 5
    assert len(b.inspect()['received']) == 2
    # Unknown/extra fields, bad destination, non-positive units.
    expect_error('extra field', lambda: b.receive(dict(m2, extra=1)))
    expect_error('wrong destination', lambda: a.receive(m2))
    expect_error('non-positive units', lambda: b.receive(
        {'id': 'A:x', 'source': 'A', 'destination': 'B', 'units': 0}))
    expect_error('bool units', lambda: b.receive(
        {'id': 'A:y', 'source': 'A', 'destination': 'B', 'units': True}))
    expect_error('same endpoint', lambda: b.receive(
        {'id': 'A:z', 'source': 'B', 'destination': 'B', 'units': 1}))
    assert b.available() == 5
    a.close()
    b.close()


@check('persistence keeps balance, transfers and usages across reopen')
def _():
    d = tmpdir()
    pa = os.path.join(d, 'a.db')
    a = Rights(pa, 'A', 5)
    a.spend('u1', 2)
    a.transfer('t1', 'B', 2)
    before = a.inspect()
    a.close()
    a = Rights(pa, 'A', 5)
    after = a.inspect()
    assert after['available'] == before['available'] == 1
    assert after['outgoing'] == before['outgoing']
    assert after['usages'] == before['usages']
    a.close()
    # Wrong site or wrong initial grant must refuse to open the existing DB.
    expect_error('reopen wrong site', lambda: Rights(pa, 'B', 5))
    expect_error('reopen wrong grant', lambda: Rights(pa, 'A', 4))
    expect_error('reopen wrong grant 2', lambda: Rights(pa, 'A', 6))


@check('sdk-only conservation via audit')
def _():
    d = tmpdir()
    pa = os.path.join(d, 'a.db')
    pb = os.path.join(d, 'b.db')
    a = Rights(pa, 'A', 5)
    b = Rights(pb, 'B', 3)
    a.spend('s1', 2)                 # unreturned spent units stand in for rows
    m = a.transfer('t1', 'B', 2)
    b.available()
    a.close()
    b.close()
    res = audit.audit(pa, pb, capacity=8)
    assert res['mode'] == 'sdk_only', res
    assert res['unreturned_units'] == 2, res
    assert res['inflight'] == 2, res
    assert res['conservation_lhs'] == 8 and res['ok'], res
    # Now receive and confirm in-flight drops, unspent rises, still 8.
    b = Rights(pb, 'B', 3)
    b.receive(m)
    b.close()
    res = audit.audit(pa, pb, capacity=8)
    assert res['inflight'] == 0 and res['unspent'] == 6, res
    assert res['conservation_lhs'] == 8 and res['ok'], res


# ---------------------------------------------------------------------------
# 2. Accurate local quota participant: integration transaction rollback
# ---------------------------------------------------------------------------
@check('integration: last row CHECK failure rolls everything back')
def _():
    import sqlite3
    d = tmpdir()
    db = sqlite3.connect(os.path.join(d, 'site_a.db'), isolation_level=None)
    db.row_factory = sqlite3.Row
    quota = LocalQuotaFixture(db, 3)
    rows = WarehouseRows(quota)
    before_avail = quota.available()
    before_rows = rows.rows()
    before_receipts = rows.receipts()
    # Third row violates the SQLite CHECK (negative value).
    bad_batch = [
        {'id': 'good1', 'value': 4},
        {'id': 'good2', 'value': 5},
        {'id': 'bad', 'value': -1},
    ]
    expect_error('CHECK failure batch', lambda: rows.import_rows('op1', bad_batch))
    assert quota.available() == before_avail, (quota.available(), before_avail)
    assert rows.rows() == before_rows
    assert rows.receipts() == before_receipts
    # No orphan usages left behind by the failed operation.
    ids = [u['usage'] for u in rows.row_usages()]
    assert not any(u.startswith('row:op1:') for u in ids), ids
    usage_rows = db.execute('SELECT id FROM fixture_usages').fetchall()
    assert not any(r[0].startswith('row:op1:') for r in usage_rows), (
        [r[0] for r in usage_rows])
    # A good batch still works afterwards.
    rows.import_rows('op2', [{'id': 'good1', 'value': 4},
                             {'id': 'good2', 'value': 5}])
    assert quota.available() == before_avail - 2
    assert {r['id'] for r in rows.rows()} == {'good1', 'good2'}
    # Replay of op2 is a no-op; conflict is rejected.
    rows.import_rows('op2', [{'id': 'good1', 'value': 4},
                             {'id': 'good2', 'value': 5}])
    assert quota.available() == before_avail - 2
    expect_error('op2 reordered rows', lambda: rows.import_rows(
        'op2', [{'id': 'good2', 'value': 5}, {'id': 'good1', 'value': 4}]))
    db.close()


@check('integration: DB constraint fails on last insert; all roll back')
def _():
    import sqlite3
    d = tmpdir()
    db = sqlite3.connect(os.path.join(d, 'site_a.db'), isolation_level=None)
    db.row_factory = sqlite3.Row
    quota = LocalQuotaFixture(db, 3)
    rows = WarehouseRows(quota)
    # Test-only trigger: a real SQLite constraint failure raised by the LAST
    # insert of the batch, after earlier INSERTs and spend() calls succeeded
    # inside the same transaction.  This exercises the storage boundary, not
    # just Python-side validation.
    db.executescript('''
        CREATE TRIGGER test_fail_bad BEFORE INSERT ON warehouse_rows
        WHEN NEW.id = 'bad'
        BEGIN SELECT RAISE(ABORT, 'test constraint'); END;
    ''')
    before_avail = quota.available()
    before_rows = rows.rows()
    before_receipts = rows.receipts()
    batch = [
        {'id': 'good1', 'value': 4},
        {'id': 'good2', 'value': 5},
        {'id': 'bad', 'value': 0},
    ]
    expect_error('last-insert constraint',
                 lambda: rows.import_rows('op1', batch),
                 exc=sqlite3.IntegrityError)
    assert quota.available() == before_avail, (quota.available(), before_avail)
    assert rows.rows() == before_rows
    assert rows.receipts() == before_receipts
    assert not any(u['usage'].startswith('row:op1:')
                   for u in rows.row_usages())
    fixture_usages = [r[0] for r in db.execute('SELECT id FROM fixture_usages')]
    assert not any(u.startswith('row:op1:') for u in fixture_usages), fixture_usages
    # The failed operation id is free for a corrected request.
    db.execute('DROP TRIGGER test_fail_bad')
    rows.import_rows('op1', [{'id': 'good1', 'value': 4},
                             {'id': 'good2', 'value': 5}])
    assert quota.available() == before_avail - 2
    assert {r['id'] for r in rows.rows()} == {'good1', 'good2'}
    db.close()


@check('integration: insufficient quota rolls the whole batch back')
def _():
    d = tmpdir()
    import sqlite3
    db = sqlite3.connect(os.path.join(d, 'q.db'), isolation_level=None)
    db.row_factory = sqlite3.Row
    quota = LocalQuotaFixture(db, 1)
    rows = WarehouseRows(quota)
    expect_error('overdraft batch', lambda: rows.import_rows(
        'op', [{'id': 'a', 'value': 1}, {'id': 'b', 'value': 2}]))
    assert quota.available() == 1
    assert rows.rows() == []
    assert rows.receipts() == []
    db.close()


# ---------------------------------------------------------------------------
# 3. Two real databases, real transport files, conservation stages
# ---------------------------------------------------------------------------
@check('receive rejects malformed message ids and does not mutate state')
def _():
    d = tmpdir()
    a = Rights(os.path.join(d, 'a.db'), 'A', 4)
    b = Rights(os.path.join(d, 'b.db'), 'B', 0)
    a.transfer('t1', 'B', 2)
    good = {'id': 'A:t1', 'source': 'A', 'destination': 'B', 'units': 2}
    expect_error('empty id', lambda: b.receive(dict(good, id='')))
    expect_error('non-text id', lambda: b.receive(dict(good, id=7)))
    assert b.available() == 0 and b.inspect()['received'] == []
    b.receive(good)
    assert b.available() == 2
    a.close()
    b.close()


@check('many transfers: arbitrary reorder and repeat, conservation')
def _():
    d = tmpdir()
    pa = os.path.join(d, 'A.db')
    pb = os.path.join(d, 'b.db')
    a = Rights(pa, 'A', 10)
    b = Rights(pb, 'B', 0)
    msgs = [a.transfer('m%d' % i, 'B', 1) for i in range(10)]
    assert a.available() == 0
    order = [7, 2, 9, 0, 5, 5, 1, 8, 3, 6, 4, 4, 2]
    for i in order:
        b.receive(msgs[i])
    assert b.available() == 10
    assert len(b.inspect()['received']) == 10
    a.close()
    b.close()
    res = audit.audit(pa, pb, capacity=10)
    assert res['inflight'] == 0 and res['unspent'] == 10, res
    assert res['ok'] and res['conservation_lhs'] == 10, res


@check('successful import retry is idempotent after balance drops')
def _():
    d = tmpdir()
    a = Rights(os.path.join(d, 'a.db'), 'A', 2)
    wa = WarehouseRows(a)
    wa.import_rows('i', [{'id': 'x', 'value': 1}])
    assert a.available() == 1
    # Spend the remaining unit using the SDK directly, so a naive
    # re-execution of the retried import would now run out of capacity.
    a.spend('direct', 1)
    assert a.available() == 0
    wa.import_rows('i', [{'id': 'x', 'value': 1}])
    assert len(wa.rows()) == 1
    assert a.available() == 0
    a.close()


@check('B-to-A transfer direction and cross-site conservation')
def _():
    d = tmpdir()
    pa = os.path.join(d, 'A.db')
    pb = os.path.join(d, 'B.db')
    a = Rights(pa, 'A', 1)
    b = Rights(pb, 'B', 4)
    m = b.transfer('back', 'A', 3)
    assert m['id'] == 'B:back' and m['source'] == 'B'
    assert b.available() == 1
    a.receive(m)
    assert a.available() == 4
    a.close()
    b.close()
    res = audit.audit(pa, pb, capacity=5)
    assert res['inflight'] == 0 and res['unspent'] == 5, res
    assert res['ok'], res


@check('audit flags a forged received message')
def _():
    d = tmpdir()
    pa = os.path.join(d, 'A.db')
    pb = os.path.join(d, 'B.db')
    a = Rights(pa, 'A', 1)
    b = Rights(pb, 'B', 0)
    b.receive({'id': 'A:forged', 'source': 'A', 'destination': 'B',
               'units': 1})
    a.close()
    b.close()
    res = audit.audit(pa, pb, capacity=1)
    assert not res['ok']
    assert any('absent from peer outbox' in i for i in res['issues']), res


@check('two-site end-to-end with real JSON handoff and audit')
def _():
    d = tmpdir()
    pa = os.path.join(d, 'A.db')
    pb = os.path.join(d, 'B.db')
    inbox = os.path.join(d, 'inbox')
    os.makedirs(inbox)

    a = Rights(pa, 'A', 2)
    b = Rights(pb, 'B', 1)
    wa = WarehouseRows(a)
    wb = WarehouseRows(b)

    def state(label):
        res = audit.audit(pa, pb, capacity=3)
        assert res['ok'], (label, res)
        return res

    # Stage 0: pristine.
    s = state('initial')
    assert (s['live_rows'], s['unspent'], s['inflight']) == (0, 3, 0)
    assert s['one_to_one_rows_usages'] is True

    # Stage 1: send-unreceived -- units in flight, neither side can spend.
    wa.import_rows('a-imp-1', [{'id': 'x', 'value': 4}])
    m1 = a.transfer('tr1', 'B', 1)
    assert a.available() == 0
    p1 = export_message(m1, os.path.join(d, 'tr1.json'))
    s = state('sent')
    assert (s['live_rows'], s['unspent'], s['inflight']) == (1, 1, 1), s
    expect_error('source cannot spend in-flight',
                 lambda: a.spend('nope', 1))
    # B owns only its own grant of 1; the in-flight unit is not spendable.
    expect_error('dest cannot spend in-flight',
                 lambda: b.spend('nope', 2))

    # Stage 2: received but source does not know; in-flight becomes zero.
    b.receive(read_message(p1))
    s = state('received')
    assert (s['live_rows'], s['unspent'], s['inflight']) == (1, 2, 0), s
    # Duplicate arrival is harmless.
    b.receive(read_message(p1))
    assert b.available() == 2
    s = state('received-dup')
    assert (s['live_rows'], s['unspent'], s['inflight']) == (1, 2, 0), s

    # Stage 3: B imports a real row using the received capacity.
    wb.import_rows('b-imp-1', [{'id': 'y', 'value': 7}])
    s = state('B-row')
    assert (s['live_rows'], s['unspent'], s['inflight']) == (2, 1, 0), s

    # Stage 4: delete and rebuild with a fresh usage/operation.
    wa.delete('a-del-1', ['x'])
    s = state('A-delete')
    assert (s['live_rows'], s['unspent'], s['inflight']) == (1, 2, 0), s
    expect_error('restored usage cannot be reused',
                 lambda: a.spend('row:a-imp-1:0', 1))
    # Rebuild the same local id with a new operation (fresh usage).
    wa.import_rows('a-imp-2', [{'id': 'x', 'value': 9}])
    s = state('A-rebuild')
    assert (s['live_rows'], s['unspent'], s['inflight']) == (2, 1, 0), s
    # Replaying the old import does not create a second row/usage.
    wa.import_rows('a-imp-1', [{'id': 'x', 'value': 4}])
    assert {r['id'] for r in wa.rows()} == {'x'}
    s = state('replay-import')
    assert (s['live_rows'], s['unspent'], s['inflight']) == (2, 1, 0), s

    # Per-row usage mapping.
    for w in (wa, wb):
        live = {r['usage'] for r in w.rows()}
        assert len(live) == len(w.rows())
        assert all(u['units'] == 1 and not u['returned']
                   for u in w.rights.inspect()['usages']
                   if u['id'] in live)

    a.close()
    b.close()


@check('two-site: delete receipt conflicts and no-op deletes')
def _():
    d = tmpdir()
    a = Rights(os.path.join(d, 'A.db'), 'A', 4)
    wa = WarehouseRows(a)
    wa.import_rows('i1', [{'id': 'x', 'value': 1}, {'id': 'y', 'value': 2}])
    assert a.available() == 2
    # Repeated identities in a delete list must not refund twice.
    wa.delete('d1', ['x', 'x'])
    assert a.available() == 3
    # Delete of an absent id is a no-op but still a receipt.
    wa.delete('d2', ['absent'])
    assert a.available() == 3
    # Replay same delete is idempotent, cannot refund again.
    wa.delete('d1', ['x', 'x'])
    assert a.available() == 3
    # Same operation id with different content is rejected.
    expect_error('delete replay different',
                 lambda: wa.delete('d1', ['y']))
    # Shared import/delete operation namespace.
    expect_error('op id reused across kinds',
                 lambda: wa.delete('i1', ['y']))
    # Array order is part of the canonical delete request: a reordered
    # request with the same elements conflicts with the d2 receipt.
    expect_error('delete order differs',
                 lambda: wa.delete('d2', ['absent', 'absent']))
    a.close()


@check('invalid inputs leave no partial state')
def _():
    d = tmpdir()
    a = Rights(os.path.join(d, 'A.db'), 'A', 3)
    wa = WarehouseRows(a)
    before = a.available()
    for bad in (
        lambda: wa.import_rows('', [{'id': 'x', 'value': 1}]),
        lambda: wa.import_rows('o', [{'id': '', 'value': 1}]),
        lambda: wa.import_rows('o', [{'id': 'x', 'value': True}]),
        lambda: wa.import_rows('o', [{'id': 'x', 'value': 1.0}]),
        lambda: wa.import_rows('o', [{'id': 'x'}]),
        lambda: wa.import_rows('o', [{'id': 'x', 'value': 1, 'z': 2}]),
        lambda: wa.import_rows('o', [{'id': 'x', 'value': 1},
                                    {'id': 'x', 'value': 2}]),
        lambda: wa.delete('', ['x']),
        lambda: wa.delete('d', ['']),
        lambda: wa.delete('d', 'x'),
    ):
        expect_error('invalid input', bad)
    assert a.available() == before
    assert wa.rows() == [] and wa.receipts() == []
    a.close()


def main():
    for name, fn in CHECKS:
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAILURES.append((name, traceback.format_exc()))
            print(f'FAIL  {name}')
        else:
            print(f'ok    {name}')
    print()
    if FAILURES:
        for name, tb in FAILURES:
            print('=' * 70)
            print(name)
            print(tb)
        print(f'{len(FAILURES)} failure(s)')
        return 1
    print(f'all {len(CHECKS)} checks passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
