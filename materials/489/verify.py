"""Comprehensive verification for the CSV transaction participant and vtab.

Covers every scenario required by the task and adds randomized differential
testing against a native ``id INTEGER PRIMARY KEY, value INTEGER NOT NULL
CHECK(value>=0)`` table over arbitrary legal operation sequences.
"""
import copy
import os
import random
import tempfile
from pathlib import Path

import apsw

from participant import TableTransactions
from virtual import CsvModule

PASS_COUNT = 0
FAIL_COUNT = 0


def check(name, condition, detail=''):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
    else:
        FAIL_COUNT += 1
        print(f'FAIL: {name} {detail}')


def fresh_folder():
    folder = Path(tempfile.mkdtemp())
    path = folder / 'data.csv'
    path.write_text('id,value\n1,10\n2,20\n')
    return folder, path


def make_db(path, limit=100):
    p = TableTransactions(path, total_limit=limit)
    db = apsw.Connection(':memory:')
    # iVersion=2 is mandatory: only version >=2 delivers savepoint callbacks.
    db.create_module('csv_external', CsvModule(p), iVersion=2)
    db.execute('CREATE VIRTUAL TABLE ext USING csv_external')
    db.execute('CREATE TABLE ordinary(id INTEGER PRIMARY KEY, '
               'marker INTEGER NOT NULL CHECK(marker>=0))')
    db.execute('INSERT INTO ordinary VALUES(1,1)')
    return p, db


# --------------------------------------------------------------- direct client
def test_direct_commit():
    _, path = fresh_folder()
    p = TableTransactions(path)
    p.begin()
    p.put(3, 30)
    p.remove(1)
    check('direct: disk unchanged mid-tx', p.disk_rows() == {1: 10, 2: 20})
    check('direct: rows() sees private', p.rows() == {2: 20, 3: 30})
    p.prepare()
    p.commit()
    check('direct: committed rows', p.disk_rows() == {2: 20, 3: 30})
    check('direct: reopen sees commit', TableTransactions(path).disk_rows() == {2: 20, 3: 30})


def test_direct_rollback_discards_all():
    _, path = fresh_folder()
    p = TableTransactions(path)
    p.begin()
    p.put(5, 50)
    p.savepoint(0)
    p.put(6, 60)
    p.rollback_to(0)
    p.rollback()
    check('direct rollback: disk untouched', p.disk_rows() == {1: 10, 2: 20})
    check('direct rollback: inactive', not p.active)


def test_direct_savepoint_alias_and_reuse():
    _, path = fresh_folder()
    p = TableTransactions(path)
    p.begin()
    p.savepoint(0)
    p.put(1, 40)
    # The saved state must not alias the live private dict.
    p.rollback_to(0)
    check('savepoint rollback restores snapshot', p.rows() == {1: 10, 2: 20})
    p.release(0)
    # Number reuse after release.
    p.savepoint(0)
    p.put(2, 25)
    p.rollback_to(0)
    check('reused number holds fresh snapshot', p.rows() == {1: 10, 2: 20})
    p.put(7, 7)
    p.prepare()
    p.commit()
    check('savepoint/reuse final commit', p.disk_rows() == {1: 10, 2: 20, 7: 7})


def test_direct_nested_savepoints():
    _, path = fresh_folder()
    p = TableTransactions(path)
    p.begin()
    p.savepoint(0)
    p.put(3, 30)
    p.savepoint(1)
    p.put(4, 40)
    # rollback to the inner mark restores the state captured when mark 1 was
    # taken: ids 1,2,3 present, id 4 not yet inserted.
    p.rollback_to(1)
    check('rollback to inner restores its snapshot',
          p.rows() == {1: 10, 2: 20, 3: 30})
    p.rollback_to(0)
    check('rollback to outer drops inner changes', p.rows() == {1: 10, 2: 20})
    p.savepoint(1)            # inner number reusable after being dropped
    p.put(5, 50)
    p.release(1)
    check('release inner keeps outer data', p.rows() == {1: 10, 2: 20, 5: 50})
    p.release(0)
    p.prepare()
    p.commit()
    check('nested savepoints committed', p.disk_rows() == {1: 10, 2: 20, 5: 50})


def test_direct_prepare_rejects_over_limit():
    _, path = fresh_folder()
    p = TableTransactions(path, total_limit=100)
    p.begin()
    p.put(9, 95)              # 10+20+95 = 125 > 100
    raised = False
    try:
        p.prepare()
    except ValueError:
        raised = True
    check('prepare rejects over limit', raised)
    check('rejected prepare leaves tx active', p.active)
    check('rejected prepare: no file change', p.disk_rows() == {1: 10, 2: 20})
    p.rollback()
    check('rollback after rejected prepare', p.disk_rows() == {1: 10, 2: 20})


# ----------------------------------------------------------------- SQL behavior
NATIVE_SCHEMA = ('CREATE TABLE t(id INTEGER PRIMARY KEY, value INTEGER NOT NULL '
                 'CHECK(value>=0))')


def native_oracle(statements, initial=((1, 10), (2, 20))):
    """Run statements on a native table; return rows, error types, tx states."""
    db = apsw.Connection(':memory:')
    db.execute(NATIVE_SCHEMA)
    db.executemany('INSERT INTO t VALUES(?,?)', initial)
    trace = []
    for sql in statements:
        err = None
        try:
            db.execute(sql)
        except apsw.Error as exc:
            err = type(exc).__name__
        trace.append((sql, err, not db.get_autocommit(),
                      list(db.execute('SELECT id,value FROM t ORDER BY id'))))
    db.close()
    return trace


def vtab_run(statements, path, initial=((1, 10), (2, 20)), limit=100):
    p = TableTransactions(path, total_limit=limit)
    db = apsw.Connection(':memory:')
    db.create_module('csv_external', CsvModule(p), iVersion=2)
    db.execute('CREATE VIRTUAL TABLE t USING csv_external')
    trace = []
    for sql in statements:
        err = None
        try:
            db.execute(sql)
        except apsw.Error as exc:
            err = type(exc).__name__
        trace.append((sql, err, not db.get_autocommit(),
                      list(db.execute('SELECT id,value FROM t ORDER BY id')),
                      p.disk_rows()))
    db.close()
    return trace, p


def test_conflict_matrix_vs_native():
    cases = [
        'INSERT INTO t VALUES(3,5)',
        'INSERT INTO t VALUES(1,40)',
        'INSERT OR IGNORE INTO t VALUES(1,40),(3,5)',
        'INSERT OR REPLACE INTO t VALUES(1,40)',
        'INSERT OR ABORT INTO t VALUES(3,5),(1,40)',
        'INSERT OR IGNORE INTO t VALUES(2,99),(1,40),(3,5)',
        'INSERT OR REPLACE INTO t VALUES(2,77),(8,8)',
        'UPDATE t SET value=15 WHERE id=1',
        'UPDATE t SET id=3 WHERE id=1',
        'UPDATE OR IGNORE t SET id=2 WHERE id=1',
        'UPDATE OR REPLACE t SET id=2 WHERE id=1',
        'UPDATE OR ABORT t SET id=3 WHERE id=2',
        'DELETE FROM t WHERE id=1',
        'UPDATE t SET id=id+1',
        'INSERT OR REPLACE INTO t VALUES(1,11),(2,22),(3,33)',
    ]
    for sql in cases:
        oracle = native_oracle(['BEGIN', sql, 'COMMIT'])
        _, path = fresh_folder()
        trace, p = vtab_run(['BEGIN', sql, 'COMMIT'], path)
        # Compare per-statement error type / tx state / visible rows.
        for (osql, oerr, otx, orows), (asql, aerr, atx, arows, adisk) in zip(oracle, trace):
            check(f'conflict {sql!r}: error', oerr == aerr, f'{oerr} vs {aerr}')
            check(f'conflict {sql!r}: tx state', otx == atx)
            check(f'conflict {sql!r}: rows', orows == arows, f'{orows} vs {arows}')
        check(f'conflict {sql!r}: disk after commit',
              p.disk_rows() == dict(oracle[-1][3]), f"{p.disk_rows()} vs {oracle[-1][3]}")
        check(f'conflict {sql!r}: reopen file',
              TableTransactions(path).disk_rows() == dict(oracle[-1][3]))


def test_multiline_late_conflict_abort():
    # Earlier legal rows of the statement must be undone on ABORT, while
    # earlier complete statements in the outer transaction survive.
    _, path = fresh_folder()
    p, db = make_db(path)
    db.execute('BEGIN')
    db.execute('INSERT INTO ext VALUES(5,5)')
    try:
        db.execute('INSERT OR ABORT INTO ext VALUES(6,6),(1,99),(7,7)')
        check('late conflict raises', False)
    except apsw.ConstraintError:
        pass
    rows = dict(db.execute('SELECT id,value FROM ext'))
    check('ABORT undoes only current statement', rows == {1: 10, 2: 20, 5: 5})
    check('ABORT keeps tx open', not db.get_autocommit())
    check('ABORT leaks nothing to disk', p.disk_rows() == {1: 10, 2: 20})
    db.execute('INSERT INTO ext VALUES(8,8)')   # legal op after error
    db.execute('COMMIT')
    check('commit after recovered statement', p.disk_rows() == {1: 10, 2: 20, 5: 5, 8: 8})


def test_id_move_semantics():
    _, path = fresh_folder()
    p, db = make_db(path)
    db.execute('BEGIN')
    db.execute('UPDATE ext SET id=3 WHERE id=1')
    check('id move keeps value', dict(db.execute('SELECT id,value FROM ext')) == {2: 20, 3: 10})
    try:
        db.execute('UPDATE ext SET id=2 WHERE id=3')  # collides with existing id
        check('duplicate move should fail', False)
    except apsw.ConstraintError:
        pass
    check('failed move leaves row in place',
          dict(db.execute('SELECT id,value FROM ext')) == {2: 20, 3: 10})
    db.execute('ROLLBACK')
    check('move rollback restores file view', p.disk_rows() == {1: 10, 2: 20})


def test_ordinary_table_constraint_then_rollback():
    _, path = fresh_folder()
    p, db = make_db(path)
    db.execute('BEGIN')
    db.execute('UPDATE ext SET value=15 WHERE id=1')
    try:
        db.execute('UPDATE ordinary SET marker=-1 WHERE id=1')
        check('ordinary CHECK should fail', False)
    except apsw.ConstraintError:
        pass
    check('tx still open after ordinary constraint error', not db.get_autocommit())
    check('ext private row not on disk yet', p.disk_rows() == {1: 10, 2: 20})
    db.execute('ROLLBACK')
    check('explicit rollback: CSV unchanged', p.disk_rows() == {1: 10, 2: 20})
    check('explicit rollback: ordinary unchanged',
          list(db.execute('SELECT * FROM ordinary')) == [(1, 1)])
    check('post-rollback ext visible rows',
          dict(db.execute('SELECT id,value FROM ext')) == {1: 10, 2: 20})


def test_prepare_rejects_commit_no_publish():
    _, path = fresh_folder()
    p, db = make_db(path, limit=100)
    db.execute('BEGIN')
    db.execute('UPDATE ordinary SET marker=2 WHERE id=1')
    db.execute('UPDATE ext SET value=90 WHERE id=1')   # total 90+20 = 110
    try:
        db.execute('COMMIT')
        check('over-limit commit should fail', False)
    except apsw.ConstraintError:
        pass
    check('failed commit returns to autocommit', db.get_autocommit())
    check('failed commit: no new CSV rows', p.disk_rows() == {1: 10, 2: 20})
    check('failed commit: ordinary rolled back by SQLite',
          list(db.execute('SELECT * FROM ordinary')) == [(1, 1)])
    check('failed commit: ext view restored',
          dict(db.execute('SELECT id,value FROM ext')) == {1: 10, 2: 20})
    check('reopen file after rejected commit',
          TableTransactions(path).disk_rows() == {1: 10, 2: 20})


def test_sql_savepoints_and_continue_after_error():
    _, path = fresh_folder()
    p, db = make_db(path)
    db.execute('BEGIN')
    db.execute('SAVEPOINT a')
    db.execute('INSERT INTO ext VALUES(3,30)')
    db.execute('SAVEPOINT b')
    db.execute('INSERT INTO ext VALUES(4,40)')
    db.execute('ROLLBACK TO a')
    check('sql rollback to savepoint',
          dict(db.execute('SELECT id,value FROM ext')) == {1: 10, 2: 20})
    check('savepoint rollback no disk leak', p.disk_rows() == {1: 10, 2: 20})
    db.execute('RELEASE a')
    # statement error after savepoint work; transaction remains usable
    try:
        db.execute('INSERT INTO ext VALUES(1,5)')
    except apsw.ConstraintError:
        pass
    check('continue after statement error', not db.get_autocommit())
    db.execute('INSERT INTO ext VALUES(9,9)')
    db.execute('COMMIT')
    check('savepoint tx final commit', p.disk_rows() == {1: 10, 2: 20, 9: 9})


def test_mixed_ordinary_and_ext_commit():
    _, path = fresh_folder()
    p, db = make_db(path)
    db.execute('BEGIN')
    db.execute('INSERT INTO ordinary VALUES(7,7)')
    db.execute('INSERT INTO ext VALUES(3,30)')
    db.execute('DELETE FROM ext WHERE id=2')
    db.execute('COMMIT')
    check('mixed commit: CSV', p.disk_rows() == {1: 10, 3: 30})
    check('mixed commit: ordinary',
          list(db.execute('SELECT id,marker FROM ordinary ORDER BY id')) == [(1, 1), (7, 7)])


def test_autocommit_statement_failure():
    _, path = fresh_folder()
    p, db = make_db(path)
    try:
        db.execute('INSERT OR ABORT INTO ext VALUES(3,30),(1,99)')
    except apsw.ConstraintError:
        pass
    check('autocommit abort: rows gone',
          dict(db.execute('SELECT id,value FROM ext')) == {1: 10, 2: 20})
    check('autocommit abort: disk clean', p.disk_rows() == {1: 10, 2: 20})
    check('autocommit restored', db.get_autocommit())
    db.execute('INSERT INTO ext VALUES(5,5)')
    check('autocommit success publishes', p.disk_rows() == {1: 10, 2: 20, 5: 5})


# ------------------------------------------------------ randomized differential
def randomized_differential(seed, steps=120):
    rng = random.Random(seed)
    folder = Path(tempfile.mkdtemp())
    vpath = folder / 'v.csv'
    vpath.write_text('id,value\n1,10\n2,20\n')
    # High limit: the total_limit rule is covered by dedicated tests, here we
    # want pure conflict/transaction semantics against the native oracle.
    p = TableTransactions(vpath, total_limit=1_000_000)
    vdb = apsw.Connection(':memory:')
    vdb.create_module('csv_external', CsvModule(p), iVersion=2)
    vdb.execute('CREATE VIRTUAL TABLE t USING csv_external')
    vdb.execute('CREATE TABLE ord(id INTEGER PRIMARY KEY, m INTEGER CHECK(m>=0))')

    ndb = apsw.Connection(':memory:')
    ndb.execute(NATIVE_SCHEMA)
    ndb.execute('INSERT INTO t VALUES(1,10),(2,20)')
    ndb.execute('CREATE TABLE ord(id INTEGER PRIMARY KEY, m INTEGER CHECK(m>=0))')

    verbs = ['ins', 'ins_ig', 'ins_rep', 'upd_val', 'upd_id',
             'upd_id_ig', 'upd_id_rep', 'del', 'ord_ins', 'ord_bad']
    in_tx = False
    sp_depth = 0
    for step in range(steps):
        verb = rng.choice(verbs)
        kid = rng.randint(1, 8)
        val = rng.randint(0, 40)
        sql = {
            'ins': f'INSERT INTO t VALUES({kid},{val})',
            'ins_ig': f'INSERT OR IGNORE INTO t VALUES({kid},{val})',
            'ins_rep': f'INSERT OR REPLACE INTO t VALUES({kid},{val})',
            'upd_val': f'UPDATE t SET value={val} WHERE id={rng.randint(1,8)}',
            'upd_id': f'UPDATE t SET id={kid} WHERE id={rng.randint(1,8)}',
            'upd_id_ig': f'UPDATE OR IGNORE t SET id={kid} WHERE id={rng.randint(1,8)}',
            'upd_id_rep': f'UPDATE OR REPLACE t SET id={kid} WHERE id={rng.randint(1,8)}',
            'del': f'DELETE FROM t WHERE id={rng.randint(1,8)}',
            'ord_ins': f'INSERT OR REPLACE INTO ord VALUES({rng.randint(1,4)},{val})',
            'ord_bad': f'INSERT INTO ord VALUES(9,-1)',
        }[verb]
        # Occasionally wrap work in transactions and savepoints.
        if not in_tx and rng.random() < 0.3:
            vdb.execute('BEGIN'); ndb.execute('BEGIN'); in_tx = True
        if in_tx and sp_depth < 3 and rng.random() < 0.2:
            vdb.execute(f'SAVEPOINT s{sp_depth}')
            ndb.execute(f'SAVEPOINT s{sp_depth}')
            sp_depth += 1
        verr = nerr = None
        try:
            vdb.execute(sql)
        except apsw.Error as exc:
            verr = type(exc).__name__
        try:
            ndb.execute(sql)
        except apsw.Error as exc:
            nerr = type(exc).__name__
        tag = f'seed={seed} step={step} sql={sql!r}'
        check(f'diff error {tag}', verr == nerr, f'{verr} vs {nerr}')
        check(f'diff tx {tag}', vdb.get_autocommit() == ndb.get_autocommit())
        vrows = list(vdb.execute('SELECT id,value FROM t ORDER BY id'))
        nrows = list(ndb.execute('SELECT id,value FROM t ORDER BY id'))
        check(f'diff rows {tag}', vrows == nrows, f'{vrows} vs {nrows}')
        vord = list(vdb.execute('SELECT id,m FROM ord ORDER BY id'))
        nord = list(ndb.execute('SELECT id,m FROM ord ORDER BY id'))
        check(f'diff ordinary {tag}', vord == nord, f'{vord} vs {nord}')
        if not in_tx:
            check(f'diff disk(autocommit) {tag}',
                  p.disk_rows() == dict(nrows), f"{p.disk_rows()} vs {nrows}")
        if in_tx and sp_depth and rng.random() < 0.25:
            target = rng.randint(0, sp_depth - 1)
            vdb.execute(f'ROLLBACK TO s{target}')
            ndb.execute(f'ROLLBACK TO s{target}')
            sp_depth = target + 1
            vdb.execute(f'RELEASE s{target}')
            ndb.execute(f'RELEASE s{target}')
            sp_depth = target
        if in_tx and rng.random() < 0.2:
            if rng.random() < 0.7:
                verr = nerr = None
                try:
                    vdb.execute('COMMIT')
                except apsw.Error as exc:
                    verr = type(exc).__name__
                try:
                    ndb.execute('COMMIT')
                except apsw.Error as exc:
                    nerr = type(exc).__name__
                check(f'diff commit error seed={seed}', verr == nerr,
                      f'{verr} vs {nerr}')
                if vdb.get_autocommit():
                    in_tx = False; sp_depth = 0
                    check(f'diff disk after commit seed={seed}',
                          p.disk_rows() ==
                          dict(ndb.execute('SELECT id,value FROM t ORDER BY id')))
            else:
                vdb.execute('ROLLBACK'); ndb.execute('ROLLBACK')
                in_tx = False; sp_depth = 0
                check(f'diff disk after rollback seed={seed}',
                      p.disk_rows() ==
                      dict(ndb.execute('SELECT id,value FROM t ORDER BY id')))
    # Drain whatever remains.
    if not vdb.get_autocommit():
        vdb.execute('ROLLBACK'); ndb.execute('ROLLBACK')
    check(f'final disk matches native seed={seed}',
          p.disk_rows() == dict(ndb.execute('SELECT id,value FROM t ORDER BY id')))
    vdb.close(); ndb.close()


def test_late_join_savepoints_and_statement_aborts():
    """All four savepoint levels exist before the vtab's first write, so the
    whole user stack must be backfilled; statement aborts and rollbacks then
    behave like the native table."""
    _, path = fresh_folder()
    p = TableTransactions(path, total_limit=10**6)
    vdb = apsw.Connection(':memory:')
    vdb.create_module('csv_external', CsvModule(p), iVersion=2)
    vdb.execute('CREATE VIRTUAL TABLE t USING csv_external')
    ndb = apsw.Connection(':memory:')
    ndb.execute(NATIVE_SCHEMA)
    ndb.execute('INSERT INTO t VALUES(1,10),(2,20)')
    script = [
        'BEGIN',
        'SAVEPOINT s0', 'SAVEPOINT s1', 'SAVEPOINT s2', 'SAVEPOINT s3',
        'INSERT INTO t VALUES(3,30)',                 # first vtab write
        'INSERT OR ABORT INTO t VALUES(4,40),(1,9)',  # aborts
        'ROLLBACK TO s2',
        'INSERT OR REPLACE INTO t VALUES(2,22)',
        'ROLLBACK TO s0',
        'RELEASE s0',
        'INSERT OR IGNORE INTO t VALUES(1,5),(5,50)',
        'UPDATE t SET id=6 WHERE id=3',
        'COMMIT',
    ]
    for sql in script:
        verr = nerr = None
        try:
            vdb.execute(sql)
        except apsw.Error as exc:
            verr = type(exc).__name__
        try:
            ndb.execute(sql)
        except apsw.Error as exc:
            nerr = type(exc).__name__
        check(f'late-join error {sql}', verr == nerr, f'{verr} vs {nerr}')
        vrows = list(vdb.execute('SELECT id,value FROM t ORDER BY id'))
        nrows = list(ndb.execute('SELECT id,value FROM t ORDER BY id'))
        check(f'late-join rows {sql}', vrows == nrows, f'{vrows} vs {nrows}')
    check('late-join disk matches native',
          p.disk_rows() == dict(ndb.execute('SELECT id,value FROM t ORDER BY id')))
    check('late-join reopen matches',
          TableTransactions(path).disk_rows() ==
          dict(ndb.execute('SELECT id,value FROM t ORDER BY id')))


def test_autocommit_savepoint_then_continue():
    """A bare SAVEPOINT starts a transaction; error then commit/rollback must
    match native behavior including disk visibility."""
    _, path = fresh_folder()
    p = TableTransactions(path, total_limit=10**6)
    vdb = apsw.Connection(':memory:')
    vdb.create_module('csv_external', CsvModule(p), iVersion=2)
    vdb.execute('CREATE VIRTUAL TABLE t USING csv_external')
    ndb = apsw.Connection(':memory:')
    ndb.execute(NATIVE_SCHEMA)
    ndb.execute('INSERT INTO t VALUES(1,10),(2,20)')
    for sql in ['SAVEPOINT a', 'INSERT INTO t VALUES(3,30)',
                'INSERT INTO t VALUES(1,5)',          # aborts
                'INSERT INTO t VALUES(4,40)',
                'RELEASE a']:
        verr = nerr = None
        try:
            vdb.execute(sql)
        except apsw.Error as exc:
            verr = type(exc).__name__
        try:
            ndb.execute(sql)
        except apsw.Error as exc:
            nerr = type(exc).__name__
        check(f'ac-sp error {sql}', verr == nerr)
        check(f'ac-sp rows {sql}',
              list(vdb.execute('SELECT id,value FROM t ORDER BY id')) ==
              list(ndb.execute('SELECT id,value FROM t ORDER BY id')))
    check('ac-sp committed to disk', p.disk_rows() ==
          dict(ndb.execute('SELECT id,value FROM t ORDER BY id')))


def main():
    test_direct_commit()
    test_direct_rollback_discards_all()
    test_direct_savepoint_alias_and_reuse()
    test_direct_nested_savepoints()
    test_direct_prepare_rejects_over_limit()
    test_conflict_matrix_vs_native()
    test_multiline_late_conflict_abort()
    test_id_move_semantics()
    test_ordinary_table_constraint_then_rollback()
    test_prepare_rejects_commit_no_publish()
    test_sql_savepoints_and_continue_after_error()
    test_mixed_ordinary_and_ext_commit()
    test_autocommit_statement_failure()
    test_late_join_savepoints_and_statement_aborts()
    test_autocommit_savepoint_then_continue()
    for seed in range(120):
        randomized_differential(seed, steps=150)
    print(f'\n{PASS_COUNT} checks passed, {FAIL_COUNT} failed')
    return FAIL_COUNT == 0


if __name__ == '__main__':
    raise SystemExit(0 if main() else 1)
