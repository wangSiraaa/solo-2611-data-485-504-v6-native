"""End-to-end reproduction for the revision graph + real migration executor.

Run: python3 demo.py

Every check below is made with independent SQLite inspections (schema and row
queries) and hand-written ancestor sets, not just by trusting state strings the
tool returns.  Databases are created in a temporary directory.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

from executor import MigrationExecutor, RevisionDefinitionConflict
from revision_graph import InvalidRevisionGraph, RevisionGraph

HERE = os.path.dirname(os.path.abspath(__file__))


def load_examples():
    with open(os.path.join(HERE, 'examples.json'), encoding='utf-8') as handle:
        return json.load(handle)


def banner(title):
    print()
    print('=' * 72)
    print(title)
    print('=' * 72)


def ancestor_set(parents, revision):
    seen, stack = set(), [revision]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(parents[current])
    return seen


def show_rows(connection, sql):
    return connection.execute(sql).fetchall()


def check(condition, message):
    status = 'ok  ' if condition else 'FAIL'
    print(f'  [{status}] {message}')
    if not condition:
        raise AssertionError(message)


def section_graph_validation():
    banner('1. Offline graph validation (invalid input changes nothing)')
    good = {'root': [], 'a': ['root'], 'b': ['root'], 'merge': ['a', 'b']}
    graph = RevisionGraph(good)
    before = ({k: list(v) for k, v in graph.parents.items()}, set(graph.applied_heads))

    bad_cases = {
        'unknown parent': {'root': [], 'x': ['missing']},
        'duplicate parent': {'root': [], 'x': ['root', 'root']},
        'cycle': {'x': ['y'], 'y': ['x']},
        'three parents': {'r': [], 'x': ['r', 'r2', 'r3'], 'r2': ['r'], 'r3': ['r']},
    }
    for name, definition in bad_cases.items():
        try:
            RevisionGraph(definition)
        except InvalidRevisionGraph as exc:
            print(f'  rejected {name}: {exc}')
        else:
            raise AssertionError(f'{name} was accepted')
    invalid_mutations = (
        lambda: graph.add_revision('injected', ['missing']),
        lambda: graph.add_revision('injected', ['root', 'root']),
        lambda: graph.add_revision('a', []),
    )
    for mutate in invalid_mutations:
        try:
            mutate()
        except InvalidRevisionGraph:
            pass
        else:
            raise AssertionError('invalid mutation accepted')
    check(before == (graph.parents, graph.applied_heads),
          'existing graph unchanged after rejected mutations')
    check(graph.heads() == ['merge'], f'heads are unreferenced: {graph.heads()}')

    for heads, why in ((['a', 'root'], 'ancestor+descendant'), (['a', 'a'], 'duplicate')):
        try:
            RevisionGraph({'root': [], 'a': ['root']}, heads)
        except InvalidRevisionGraph:
            print(f'  rejected applied_heads {heads} ({why})')
        else:
            raise AssertionError('invalid applied_heads accepted')


def section_planning():
    banner('2. Multi-head planning, merge deduplication, success advancement')
    examples = load_examples()
    parents = examples['parents']
    graph = RevisionGraph(parents)

    plan = graph.plan(['merge'])
    check(plan == ['root', 'a', 'b', 'merge'], f'full plan parents-first: {plan}')
    check(len(plan) == len(set(plan)), 'each revision planned once (shared ancestors)')

    graph.record_success('root')
    graph.record_success('a')
    check(sorted(graph.applied_heads) == ['a'], f'heads after a: {sorted(graph.applied_heads)}')
    try:
        graph.record_success('merge')
    except InvalidRevisionGraph:
        print('  rejected merge before all parents successful')
    else:
        raise AssertionError('merge recorded without parent b')
    check(sorted(graph.applied_heads) == ['a'], 'heads unchanged after rejected record')

    graph2 = RevisionGraph(parents)
    for revision in ('root', 'a', 'b'):
        graph2.record_success(revision)
    check(sorted(graph2.applied_heads) == ['a', 'b'],
          f'independent heads preserved: {sorted(graph2.applied_heads)}')
    check(graph2.plan(['merge']) == ['merge'],
          'successful side branches are not replanned')
    graph2.record_success('merge')
    check(graph2.applied_heads == {'merge'}, 'merge replaces both covered heads')

    # two independent targets share root exactly once
    graph3 = RevisionGraph(parents)
    leaves = graph3.plan(['a', 'b'])
    check(leaves == ['root', 'a', 'b'], f'two targets dedupe shared ancestor: {leaves}')


def section_linear_upgrade():
    banner('3a. Normal linear graph upgrade')
    examples = load_examples()
    parents = examples['linear_parents']
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, 'linear.db')
    connection = sqlite3.connect(path)
    connection.executescript('CREATE TABLE steps(step TEXT PRIMARY KEY);')
    connection.commit()
    revisions = {
        name: [{'kind': 'sql', 'sql': f"INSERT INTO steps VALUES('{name}')"}]
        for name in parents
    }
    executor = MigrationExecutor(connection)
    graph = RevisionGraph(parents)
    receipts = executor.upgrade(graph, revisions, ['l3'])
    check([r['revision'] for r in receipts] == ['root', 'l1', 'l2', 'l3'],
          f'linear plan in order: {[r["revision"] for r in receipts]}')
    rows = show_rows(connection, 'SELECT step FROM steps ORDER BY step')
    check(rows == [('l1',), ('l2',), ('l3',), ('root',)],
          f'each linear step committed once: {rows}')
    check(graph.applied_heads == {'l3'}, 'linear head advanced to l3')
    connection.close()


def section_diamond_upgrade():
    banner('3. Real SQLite diamond upgrade, no failure, merge effective')
    examples = load_examples()
    parents = examples['parents']
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, 'diamond.db')
    connection = sqlite3.connect(path)
    connection.executescript('CREATE TABLE pipeline(stage TEXT PRIMARY KEY);')
    connection.commit()

    revisions = {
        'root': [{'kind': 'sql', 'sql': "INSERT INTO pipeline VALUES('root')"}],
        'a': [{'kind': 'sql', 'sql': "INSERT INTO pipeline VALUES('a')"}],
        'b': [{'kind': 'sql', 'sql': "INSERT INTO pipeline VALUES('b')"}],
        'merge': [{'kind': 'sql', 'sql': "INSERT INTO pipeline VALUES('merge')"}],
    }
    executor = MigrationExecutor(connection)
    graph = RevisionGraph(parents)

    first = executor.upgrade(graph, revisions, ['a', 'b'])
    check([r['revision'] for r in first] == ['root', 'a', 'b'],
          f'plan for a,b executed: {[r["revision"] for r in first]}')
    rows = show_rows(connection, 'SELECT stage FROM pipeline ORDER BY stage')
    check(rows == [('a',), ('b',), ('root',)], f'independent SQL rows committed: {rows}')

    second = executor.upgrade(graph, revisions, ['merge'])
    check([r['revision'] for r in second] == ['merge'],
          'merge upgrade runs only the not-yet-successful revision')
    rows = show_rows(connection, 'SELECT stage FROM pipeline ORDER BY stage')
    check(rows == [('a',), ('b',), ('merge',), ('root',)],
          f'merge row present exactly once: {rows}')
    check(sorted(graph.applied_heads) == ['merge'], 'single merge head')

    # each ancestor really ran once: compare against a hand-written closure
    expected_closure = {
        'root': {'root'},
        'a': {'root', 'a'},
        'b': {'root', 'b'},
        'merge': {'root', 'a', 'b', 'merge'},
    }
    for revision, expected in expected_closure.items():
        check(ancestor_set(parents, revision) == expected,
              f'hand ancestor set for {revision}: {sorted(expected)}')
    runs = {r['revision_id']: r for r in executor.run_records()}
    check(all(runs[name]['state'] == 'complete' for name in revisions),
          'every revision has a complete run record')
    check(all(len([r for r in executor.run_records(name)]) == 1
              for name in revisions),
          'each revision ran exactly once (no duplicate attempts)')
    counts = dict(connection.execute(
        'SELECT stage, COUNT(*) FROM pipeline GROUP BY stage').fetchall())
    check(counts == {'root': 1, 'a': 1, 'b': 1, 'merge': 1},
          f'no duplicate ancestor effects: {counts}')
    # An independent schema/row query, not the executor's own state strings.
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check('pipeline' in tables and connection.execute(
        'SELECT COUNT(*) FROM pipeline').fetchone()[0] == 4,
        'independent schema/row count confirms four committed stages')

    # reopen: heads reload and a third upgrade is a no-op
    connection.close()
    connection = sqlite3.connect(path)
    executor = MigrationExecutor(connection)
    reloaded = RevisionGraph(parents)
    heads = executor.load_heads(reloaded, stored=True)
    check(heads == ['merge'], f'durable applied heads reload after reopen: {heads}')
    check(reloaded.plan(['merge']) == [], 'no work to plan after reload')
    third = executor.upgrade(reloaded, revisions, ['merge'])
    check(third == [], 'replayed upgrade executes nothing')
    counts = dict(connection.execute(
        'SELECT stage, COUNT(*) FROM pipeline GROUP BY stage').fetchall())
    check(counts == {'root': 1, 'a': 1, 'b': 1, 'merge': 1},
          f'no duplicate INSERT after reopen: {counts}')

    # durable heads equal an independent recomputation from complete records
    independent = RevisionGraph(parents)
    recomputed = executor.load_heads(independent)
    check(sorted(recomputed) == sorted(executor.stored_heads()),
          f'graph plan and run records agree: {recomputed} == {executor.stored_heads()}')
    connection.close()
    print(f'\n  database kept at: {path}')


def section_partial_material():
    banner('4. Partial material: INSERT -> VACUUM -> invalid SQL')
    examples = load_examples()
    partial = examples['partial']
    directory = tempfile.mkdtemp()

    # ---- 4a. Direct execute: no graph involved ----
    print('\n  4a. Direct execute (executor used without a revision graph)')
    direct_path = os.path.join(directory, 'direct.db')
    connection = sqlite3.connect(direct_path)
    connection.executescript(
        'CREATE TABLE facts(id INTEGER PRIMARY KEY, label TEXT);'
        'CREATE TABLE untouched(ok INTEGER);')
    connection.commit()

    executor = MigrationExecutor(connection)
    receipt = executor.execute('prefix-9', partial)
    print('  receipt:')
    print(json.dumps({k: receipt[k] for k in (
        'state', 'completed_operations', 'committed_sql_segments',
        'external_operations', 'rolled_back_operations', 'failed_operation',
        'failed_phase', 'error')}, indent=4))

    check(receipt['state'] == 'partial', 'state is partial, not a full rollback')
    check(receipt['completed_operations'] == [0, 1], 'completed: INSERT segment + VACUUM')
    check(receipt['committed_sql_segments'] == [{'operations': [0]}],
          'INSERT segment reported committed before VACUUM')
    check(receipt['external_operations'] == [1], 'VACUUM reported as completed outside a tx')
    check(receipt['failed_operation'] == 2, 'failure points at the invalid SQL')
    check(receipt['rolled_back_operations'] == [],
          'nothing was open to roll back at failure time')

    rows = show_rows(connection, 'SELECT id, label FROM facts ORDER BY id')
    check(rows == [(9, 'prefix')], f'pre-VACUUM INSERT really persisted: {rows}')
    connection.close()

    # reopen the direct database: replay is blocked, definition is pinned
    connection = sqlite3.connect(direct_path)
    executor = MigrationExecutor(connection)
    before_rows = show_rows(connection, 'SELECT id, label FROM facts ORDER BY id')
    replay = executor.execute('prefix-9', partial)
    check(replay['state'] == 'blocked', 'direct execute after reopen refuses replay')
    check(replay['existing_runs'][0]['state'] == 'partial',
          'blocked receipt returns the existing evidence')
    after_rows = show_rows(connection, 'SELECT id, label FROM facts ORDER BY id')
    check(after_rows == before_rows, f'no repeated INSERT: {after_rows}')
    changed = list(partial)
    changed[0] = {'kind': 'sql', 'sql': "INSERT INTO facts VALUES(9,'changed')"}
    try:
        executor.execute('prefix-9', changed)
    except RevisionDefinitionConflict as exc:
        check(exc.requested_digest != exc.stored_digest,
              'same id with a different definition is rejected before any SQL')
    else:
        raise AssertionError('definition change was accepted')
    check(show_rows(connection, 'SELECT id, label FROM facts ORDER BY id') == before_rows,
          'definition change wrote nothing')
    records = executor.run_records('prefix-9')
    check([r['state'] for r in records] == ['partial'],
          f'durable run record survives reopen: {[(r["attempt"], r["state"]) for r in records]}')
    connection.close()

    # ---- 4b. Graph-aware upgrade on a fresh database ----
    print('\n  4b. upgrade through graph root -> prefix-9')
    upgrade_path = os.path.join(directory, 'upgrade.db')
    connection = sqlite3.connect(upgrade_path)
    connection.executescript(
        'CREATE TABLE facts(id INTEGER PRIMARY KEY, label TEXT);'
        'CREATE TABLE root_marker(x);')
    connection.commit()
    executor = MigrationExecutor(connection)
    graph = RevisionGraph({'root': [], 'prefix-9': ['root']})
    revisions = {
        'root': [{'kind': 'sql', 'sql': 'INSERT INTO root_marker VALUES(1)'}],
        'prefix-9': partial,
    }
    receipts = executor.upgrade(graph, revisions, ['prefix-9'])
    check([r['state'] for r in receipts] == ['complete', 'partial'],
          f'upgrade reaches partial and stops: {[r["state"] for r in receipts]}')
    check(graph.applied_heads == {'root'}, 'failed revision is not an applied head')
    check(executor.complete_revisions() == ['root'],
          f'failed revision not stamped complete: {executor.complete_revisions()}')
    check(executor.stored_heads() == ['root'],
          f'persisted heads stay at root: {executor.stored_heads()}')
    check(show_rows(connection, 'SELECT id, label FROM facts ORDER BY id') == [(9, 'prefix')],
          'the pre-VACUUM INSERT from the failed revision is real')
    connection.close()

    # reopen: heads reload, upgrade stops at the failed revision
    connection = sqlite3.connect(upgrade_path)
    executor = MigrationExecutor(connection)
    graph = RevisionGraph({'root': [], 'prefix-9': ['root']})
    heads = executor.load_heads(graph, stored=True)
    check(heads == ['root'], f'after reopen applied heads still root: {heads}')
    before_rows = show_rows(connection, 'SELECT id, label FROM facts ORDER BY id')
    blocked = executor.upgrade(graph, revisions, ['prefix-9'])
    check(blocked[0]['state'] == 'blocked',
          'reopen upgrade blocks the partial revision')
    check(blocked[0]['reason'] == 'partial_or_incomplete_attempt',
          'block reason names the existing partial attempt')
    check(blocked[0]['existing_runs'][0]['state'] == 'partial',
          'existing evidence is returned')
    after_rows = show_rows(connection, 'SELECT id, label FROM facts ORDER BY id')
    check(after_rows == before_rows, f'no repeated INSERT after reopen: {after_rows}')
    check(graph.applied_heads == {'root'}, 'graph head remains root after blocked run')
    connection.close()
    print(f'\n  databases kept under: {directory}')


def section_clean_failure_retry():
    banner('5. Clean failure needs an explicit retry; no committed side effects')
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, 'failed.db')
    connection = sqlite3.connect(path)
    connection.executescript('CREATE TABLE facts(id INTEGER PRIMARY KEY, label TEXT);')
    connection.commit()
    executor = MigrationExecutor(connection)
    operations = [
        {'kind': 'sql', 'sql': "INSERT INTO facts VALUES(1,'x')"},
        {'kind': 'sql', 'sql': 'INSERT INTO absent VALUES(1)'},
    ]
    first = executor.execute('rev', operations)
    check(first['state'] == 'failed', 'clean failure state is failed')
    check(first['completed_operations'] == [] and first['committed_sql_segments'] == [],
          'no committed SQL claimed')
    check(first['rolled_back_operations'] == [0],
          'the uncommitted INSERT segment was rolled back')
    check(show_rows(connection, 'SELECT * FROM facts') == [], 'no row remains')

    blocked = executor.execute('rev', operations)
    check(blocked['state'] == 'blocked'
          and blocked['reason'] == 'previous_failure_requires_explicit_retry',
          'replay after clean failure is blocked by default')

    connection.execute('CREATE TABLE absent(x)')
    connection.commit()
    retried = executor.execute('rev', operations, retry=True)
    check(retried['state'] == 'complete' and retried['attempt'] == 2,
          f'explicit retry after external fix: {retried["state"]} attempt {retried["attempt"]}')
    check(show_rows(connection, 'SELECT id FROM facts') == [(1,)],
          'retry applied the INSERT exactly once')
    records = executor.run_records('rev')
    check([r['state'] for r in records] == ['failed', 'complete'],
          f'original failure evidence preserved: {[r["state"] for r in records]}')
    connection.close()


if __name__ == '__main__':
    section_graph_validation()
    section_planning()
    section_linear_upgrade()
    section_diamond_upgrade()
    section_partial_material()
    section_clean_failure_retry()
    banner('All checks passed')
