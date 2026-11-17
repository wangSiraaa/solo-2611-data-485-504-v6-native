"""Staged evidence walkthrough: two real SQLite databases + real JSON files.

Run with:  python3 materials/503/demo.py

Each stage prints values derived independently from the initial grants, the
raw persisted transfer messages and the successful row operations -- never
from the SDK's own ``available()`` output.  The final conservation line is
``live_rows + unspent + inflight == K`` (integration mode).
"""
import os
import tempfile

import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from rights import Rights  # noqa: E402
from warehouse_rows import WarehouseRows  # noqa: E402
from transport import export_message, read_message  # noqa: E402
import audit  # noqa: E402

GRANT = {'A': 2, 'B': 1}
K = sum(GRANT.values())


def show(label, res):
    print(
        f'{label:22s} live_rows={res["live_rows"]} '
        f'unspent={res["unspent"]} inflight={res["inflight"]} '
        f'-> {res["live_rows"]} + {res["unspent"]} + {res["inflight"]} '
        f'= {res["conservation_lhs"]} (K={res["capacity"]}, '
        f'ok={res["ok"]})'
    )
    if not res['ok']:
        print('   issues:', res['issues'])
    return res


def main():
    d = tempfile.mkdtemp(prefix='rights503-demo-')
    pa = os.path.join(d, 'A.db')
    pb = os.path.join(d, 'B.db')
    print('dir:', d)
    print('initial grants:', GRANT, 'K =', K)
    print()

    a = Rights(pa, 'A', GRANT['A'])
    b = Rights(pb, 'B', GRANT['B'])
    wa = WarehouseRows(a)
    wb = WarehouseRows(b)

    show('0 pristine', audit.audit(pa, pb, capacity=K))

    # 1. A creates a row, then sends 1 unit; not yet received.
    wa.import_rows('a-imp-1', [{'id': 'x', 'value': 4}])
    msg = a.transfer('tr1', 'B', 1)
    path = export_message(msg, os.path.join(d, 'tr1.json'))
    print('exported:', open(path, encoding='utf-8').read().strip())
    res = show('1 sent-unreceived', audit.audit(pa, pb, capacity=K))
    print('   inflight ids:', res['inflight_ids'])

    # 2. B receives before the source hears anything (source unknown state).
    b.receive(read_message(path))
    res = show('2 received', audit.audit(pa, pb, capacity=K))
    print('   delivered ids:', res['delivered_ids'])

    # 3. Duplicate arrival changes nothing.
    b.receive(read_message(path))
    show('3 duplicate arrival', audit.audit(pa, pb, capacity=K))

    # 4. B stores a real row from the received capacity.
    wb.import_rows('b-imp-1', [{'id': 'y', 'value': 7}])
    show('4 B real row', audit.audit(pa, pb, capacity=K))

    # 5. Delete A's row: usage is returned exactly once.
    wa.delete('a-del-1', ['x'])
    show('5 A delete', audit.audit(pa, pb, capacity=K))
    print('   A usages:', a.inspect()['usages'])

    # 6. Rebuild the same local id under a fresh operation/usage.
    wa.import_rows('a-imp-2', [{'id': 'x', 'value': 9}])
    res = show('6 A rebuild', audit.audit(pa, pb, capacity=K))
    print('   A rows:', res['sites']['A']['rows'])
    print('   B rows:', res['sites']['B']['rows'])

    # 7. Close and reopen: state survives.
    a.close()
    b.close()
    a = Rights(pa, 'A', GRANT['A'])
    b = Rights(pb, 'B', GRANT['B'])
    wa = WarehouseRows(a)
    wb = WarehouseRows(b)
    res = show('7 reopened', audit.audit(pa, pb, capacity=K))
    print('   A row ids:', [r['id'] for r in wa.rows()],
          'B row ids:', [r['id'] for r in wb.rows()])
    print()
    print('final audit ok =', res['ok'])
    a.close()
    b.close()
    return 0 if res['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
