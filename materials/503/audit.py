"""Read-only, after-the-fact conservation audit across two site databases.

The audit reads only persisted state.  It never calls the SDK's balance
methods and never consults a source-side acknowledgement.  In-flight units are
derived per transfer message: a persisted outgoing message counts as in flight
unless the destination's own received ledger already contains the same id.

Two modes:

* integration mode (warehouse tables exist): ``live_rows + unspent + inflight
  == capacity``; every live row must own exactly one independent unreturned
  one-unit usage and every unreturned usage must belong to a live row.
* SDK-only mode (no warehouse tables): the actual unreturned spent units stand
  in for live rows, so ``unreturned_units + unspent + inflight == capacity``.
"""
import json
import sqlite3
from pathlib import Path

try:  # works both as a flat module and as part of a namespace package
    from rights import canonical
except ImportError:  # pragma: no cover - package-style import
    from .rights import canonical


def _connect_ro(path):
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    return db


def _has_table(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _read_site(path):
    db = _connect_ro(path)
    try:
        meta_rows = [dict(r) for r in db.execute('SELECT * FROM rights_meta')]
        if len(meta_rows) != 1:
            raise ValueError('expected exactly one rights_meta row in ' + str(path))
        meta = meta_rows[0]
        outgoing = [
            json.loads(r['payload'])
            for r in db.execute('SELECT payload FROM outgoing ORDER BY id')
        ]
        received = [
            json.loads(r['payload'])
            for r in db.execute('SELECT payload FROM received ORDER BY id')
        ]
        usages = [
            dict(r) for r in db.execute('SELECT * FROM usages ORDER BY id')
        ]
        rows = []
        if _has_table(db, 'warehouse_rows'):
            rows = [
                dict(r) for r in db.execute(
                    'SELECT id, value, usage FROM warehouse_rows ORDER BY id'
                )
            ]
        row_usages = []
        if _has_table(db, 'row_usages'):
            row_usages = [
                dict(r) for r in db.execute(
                    'SELECT usage, row_id, active FROM row_usages ORDER BY usage'
                )
            ]
        receipts = 0
        if _has_table(db, 'row_receipts'):
            receipts = db.execute(
                'SELECT COUNT(*) FROM row_receipts'
            ).fetchone()[0]
        return {
            'site': meta['site'],
            'initial_grant': meta['initial_grant'],
            'available': meta['available'],
            'outgoing_units_counter': meta['outgoing_units'],
            'received_units_counter': meta['received_units'],
            'outgoing': outgoing,
            'received': received,
            'usages': usages,
            'rows': rows,
            'row_usages': row_usages,
            'receipt_count': receipts,
            'has_warehouse_rows': _has_table(db, 'warehouse_rows'),
        }
    finally:
        db.close()


def _valid_transfer_message(message):
    return (
        isinstance(message, dict)
        and set(message.keys()) == {'id', 'source', 'destination', 'units'}
        and isinstance(message.get('id'), str) and message['id'] != ''
        and message.get('source') in ('A', 'B')
        and message.get('destination') in ('A', 'B')
        and message['source'] != message['destination']
        and type(message.get('units')) is int and message['units'] > 0
    )


def audit(path_a, path_b, capacity=None):
    states = {
        'A': _read_site(path_a),
        'B': _read_site(path_b),
    }
    issues = []

    # -- per-site ledger counters and local balance identity -------------
    for site, state in states.items():
        out_sum = sum(m['units'] for m in state['outgoing'])
        rec_sum = sum(m['units'] for m in state['received'])
        unreturned_units = sum(
            u['units'] for u in state['usages'] if not u['returned']
        )
        state['outgoing_units'] = out_sum
        state['received_units'] = rec_sum
        state['unreturned_units'] = unreturned_units
        state['unreturned_usages'] = [
            u['id'] for u in state['usages'] if not u['returned']
        ]
        if state['outgoing_units_counter'] != out_sum:
            issues.append(f'{site}: outgoing counter mismatch')
        if state['received_units_counter'] != rec_sum:
            issues.append(f'{site}: received counter mismatch')
        expected_available = (
            state['initial_grant'] - unreturned_units + rec_sum - out_sum
        )
        state['expected_available_from_ledgers'] = expected_available
        if state['available'] != expected_available:
            issues.append(f'{site}: available disagrees with raw ledgers')
        if state['available'] < 0:
            issues.append(f'{site}: negative available balance')

    # -- in-flight is per message, using the destination's receive ledger -
    received_by_site = {
        site: {m['id']: m for m in state['received']}
        for site, state in states.items()
    }
    outgoing_by_site = {
        site: {m['id']: m for m in state['outgoing']}
        for site, state in states.items()
    }
    inflight = 0
    delivered = []
    inflight_ids = []
    for site, state in states.items():
        other = 'B' if site == 'A' else 'A'
        for msg in state['outgoing']:
            if not _valid_transfer_message(msg):
                issues.append(f'{site}: malformed outgoing message')
                continue
            if msg['source'] != site or msg['destination'] != other:
                issues.append(f'{site}: outgoing message has wrong endpoints')
                continue
            dest_received = received_by_site[other]
            if msg['id'] in dest_received:
                if canonical(dest_received[msg['id']]) != canonical(msg):
                    issues.append(
                        f'{site}: delivered message content differs at destination'
                    )
                else:
                    delivered.append(msg['id'])
            else:
                inflight += msg['units']
                inflight_ids.append(msg['id'])

    # -- received messages must correspond to the peer's outgoing ledger --
    for site, state in states.items():
        other = 'B' if site == 'A' else 'A'
        for msg in state['received']:
            if not _valid_transfer_message(msg):
                issues.append(f'{site}: malformed received message')
                continue
            if msg['destination'] != site or msg['source'] != other:
                issues.append(f'{site}: received message has wrong endpoints')
                continue
            peer = outgoing_by_site[other]
            if msg['id'] not in peer:
                issues.append(f'{site}: received message absent from peer outbox')
            elif canonical(peer[msg['id']]) != canonical(msg):
                issues.append(f'{site}: received message differs from peer outbox')

    unspent_stored = sum(state['available'] for state in states.values())
    # Use the balance derived from raw persisted ledgers, not the SDK's own
    # balance output, as the conservation operand.
    unspent = sum(
        state['expected_available_from_ledgers'] for state in states.values()
    )
    live_rows = sum(len(state['rows']) for state in states.values())
    unreturned_units = sum(
        state['unreturned_units'] for state in states.values()
    )

    # -- one-to-one live row / unreturned usage mapping ------------------
    if any(state['has_warehouse_rows'] for state in states.values()):
        for site, state in states.items():
            row_usage_ids = {row['usage'] for row in state['rows']}
            unreturned_ids = set(state['unreturned_usages'])
            if row_usage_ids != unreturned_ids:
                issues.append(
                    f'{site}: live-row usages are not one-to-one with '
                    'unreturned usages'
                )
            by_id = {u['id']: u for u in state['usages']}
            for row in state['rows']:
                usage = by_id.get(row['usage'])
                if usage is None:
                    issues.append(f'{site}: row {row["id"]} has unknown usage')
                elif usage['returned']:
                    issues.append(f'{site}: row {row["id"]} uses returned usage')
                elif usage['units'] != 1:
                    issues.append(f'{site}: row {row["id"]} usage not one unit')
            active_map = {
                ru['usage']: ru for ru in state['row_usages'] if ru['active']
            }
            if set(active_map) != row_usage_ids:
                issues.append(f'{site}: row_usages active set mismatch')
        conservation_lhs = live_rows + unspent + inflight
        mode = 'integration'
    else:
        conservation_lhs = unreturned_units + unspent + inflight
        mode = 'sdk_only'

    if capacity is None:
        capacity = sum(state['initial_grant'] for state in states.values())
    initial_total = sum(state['initial_grant'] for state in states.values())
    if initial_total != capacity:
        issues.append('initial grants sum does not equal capacity')

    result = {
        'mode': mode,
        'capacity': capacity,
        'initial_grants': {
            site: state['initial_grant'] for site, state in states.items()
        },
        'live_rows': live_rows,
        'unspent': unspent,
        'unspent_stored': unspent_stored,
        'inflight': inflight,
        'inflight_ids': sorted(inflight_ids),
        'delivered_ids': sorted(delivered),
        'unreturned_units': unreturned_units,
        'conservation_lhs': conservation_lhs,
        'conservation_holds': conservation_lhs == capacity,
        'one_to_one_rows_usages': (
            None if mode == 'sdk_only' else
            live_rows == unreturned_units
        ),
        'sites': states,
        'issues': issues,
        'ok': conservation_lhs == capacity and not issues,
    }
    return result
