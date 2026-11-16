"""Walk-through demo: direct conditional writes and batch orchestration.

Run from this directory:

    python3 demo.py

Uses a temporary SQLite database and touches nothing else.
"""
import json
import tempfile
from pathlib import Path

from store import Store, StoreError
import batch

with tempfile.TemporaryDirectory(prefix='conditional-import-') as folder:
    store = Store(Path(folder) / 'work.db')

    # ---- existing direct path: single create/update ----------------------
    print('== direct single-item writes ==')
    first = store.create('single', 3)
    print('create        ->', first)
    second = store.update('single', 4, first['etag'])
    print('update        ->', second)
    print('stale etag    ->', end=' ')
    try:
        store.update('single', 5, first['etag'])
    except StoreError as exc:
        print(exc.code)
    print('no if_match   ->', end=' ')
    try:
        store.delete('single', None)
    except StoreError as exc:
        print(exc.code)
    print('read          ->', store.read('single'))

    # ---- existing batch path: a no-failure reference group ----------------
    examples = json.loads(Path(__file__).with_name('examples.json').read_text())
    batch_store = Store(Path(folder) / 'batch.db')
    print('\n== normal batch (same-group result references) ==')
    normal = batch.run(batch_store, examples['normal'])
    print(json.dumps(normal, ensure_ascii=False, indent=2))

    value_store = Store(Path(folder) / 'value_ref.db')
    print('\n== value reference across committed groups ==')
    value_ref = batch.run(value_store, examples['value_reference'])
    print(json.dumps(value_ref, ensure_ascii=False, indent=2))

    # ---- group rollback: failed group, dependency isolation, independent Z -
    fail_store = Store(Path(folder) / 'failed.db')
    print('\n== failed group (bad value), dependent and independent groups ==')
    failed = batch.run(fail_store, examples['failed_group'])
    print(json.dumps(failed, ensure_ascii=False, indent=2))
    rows = list(fail_store.db.execute(
        'SELECT key, value, ver FROM records ORDER BY key'))
    print('actual rows   ->', rows)
    assert not any(key == 'A' for key, _, _ in rows), 'rolled-back A leaked!'
    assert any(key == 'Z' for key, _, _ in rows), 'independent Z must commit'

    # connection still healthy after the failed batch
    print('post-batch read ->', fail_store.read('Z'))

    store.close()
    batch_store.close()
    value_store.close()
    fail_store.close()
    print('\nOK')
