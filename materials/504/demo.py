"""Step-by-step verification with independently stated expected state.

Three suites:

1. ``quality``  -- real batches, duplicate run, missing input, sequence gap and
   a late old run, without any incident service.
2. ``faults``   -- explicit external fault observations, ACK, silence expiry,
   recovery then reappearance with a new episode identity.
3. ``e2e``      -- one check through the bridge: a real FAIL stays queryable
   during silence, re-delivering one failed run does not open a second
   episode, and a recurrence during silence does not inherit the old ACK.

Each step prints row evidence, current quality, episode identity and
notification identity; every observation is asserted against the expected
table below, not against the implementation's own state.
"""
import json
from pathlib import Path

from incident_service import IncidentService, IncidentError
from pipeline import QualityIncidentBridge
from quality_runs import QualityRuns, QualityInputError

EXAMPLES = json.loads(Path(__file__).with_name('examples.json').read_text())


def show(title, rows, columns):
    print('\n== %s ==' % title)
    widths = [len(name) for name in columns]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    print('  '.join(name.ljust(widths[i]) for i, name in enumerate(columns)))
    print('  '.join('-' * widths[i] for i in range(len(columns))))
    for row in rows:
        print('  '.join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def quality_row(run):
    return (run['run_id'], run['sequence'], run['status'],
            ','.join(map(str, run['bad_row_ids'])) or '-',
            ','.join(map(str, run['missing_ids'])) or '-',
            run['observed_rows'], run['complete'],
            run['fail_streak'], run['pass_streak'], run['observation'],
            'yes' if run['advances_frontier'] else 'late')


QUALITY_COLUMNS = ['run', 'seq', 'row-status', 'bad', 'missing', 'rows',
                   'complete', 'fail#', 'pass#', 'obs', 'frontier']


# --------------------------------------------------------------- suite 1
def suite_quality():
    check = EXAMPLES['check']
    expected_ids = EXAMPLES['expected_ids']
    good, bad = EXAMPLES['good_rows'], EXAMPLES['bad_rows']
    q = QualityRuns()

    q.ingest(check, 'r1', 1, good, expected_ids, True)
    q.ingest(check, 'r2', 2, good, expected_ids, True)
    duplicate = q.ingest(check, 'r2', 2, good, expected_ids, True)  # same content
    q.ingest(check, 'r3', 4, good, expected_ids, True)             # gap at seq 3
    q.ingest(check, 'r4', 5, [good[0]], expected_ids, False)        # missing + incomplete
    q.ingest(check, 'r5', 6, [good[0]], expected_ids, True)         # missing, complete
    q.ingest(check, 'late', 3, good, expected_ids, True)            # late old run

    report = q.report(check)
    actual = [quality_row(run) for run in report['runs']]
    expected = [
        ('r1', 1, 'PASS', '-', '-', 2, True, 0, 1, 'UNKNOWN', 'yes'),
        ('r2', 2, 'PASS', '-', '-', 2, True, 0, 2, 'HEALTHY', 'yes'),
        ('late', 3, 'PASS', '-', '-', 2, True, 0, 0, 'UNKNOWN', 'late'),
        ('r3', 4, 'PASS', '-', '-', 2, True, 0, 1, 'UNKNOWN', 'yes'),
        ('r4', 5, 'UNKNOWN', '-', '2', 1, False, 0, 0, 'UNKNOWN', 'yes'),
        ('r5', 6, 'FAIL', '-', '2', 1, True, 1, 0, 'UNKNOWN', 'yes'),
    ]
    assert actual == expected, (actual, expected)
    assert duplicate == q.run(check, 'r2'), 'duplicate returned a different record'

    # The late run stays in history but does not rewrite the current result.
    assert report['current']['sequence'] == 6 and report['current']['status'] == 'FAIL'

    # Conflict and sequence-reuse are rejected without partial change.
    before = len(report['runs'])
    for action in (
        lambda: q.ingest(check, 'r2', 2, bad, expected_ids, True),        # same id, new content
        lambda: q.ingest(check, 'other', 4, good, expected_ids, True),    # sequence reuse
        lambda: q.ingest(check, 'x', 7, [{'id': 9, 'value': 1}], expected_ids, True),
    ):
        try:
            action()
        except QualityInputError:
            pass
        else:
            raise AssertionError('invalid input was accepted')
    assert len(q.report(check)['runs']) == before

    # Empty expected ids: complete is a real empty batch, incomplete is unknown.
    empty_ok = q.ingest('empty_complete', 'e1', 1, [], [], True)
    empty_unknown = q.ingest('empty_incomplete', 'e1', 1, [], [], False)
    assert empty_ok['status'] == 'PASS' and empty_unknown['status'] == 'UNKNOWN'

    # Report values are copies, not aliases into internal state.
    report['current']['status'] = 'TAMPERED'
    fresh = q.report(check)
    assert fresh['current']['status'] == 'FAIL'

    show('quality evidence (check=%s)' % check,
         [quality_row(run) for run in fresh['runs']], QUALITY_COLUMNS)
    print('  current: seq=%s status=%s observation=%s' % (
        fresh['current']['sequence'], fresh['current']['status'],
        fresh['current']['observation']))
    print('  duplicate r2 counter unchanged: pass_streak=%d' % duplicate['pass_streak'])
    print('  empty complete=True -> %s ; empty complete=False -> %s'
          % (empty_ok['status'], empty_unknown['status']))
    return actual, expected


# --------------------------------------------------------------- suite 2
def incident_row(key, snapshot, notification_ids):
    if snapshot is None:
        return (key, '-', '-', '-', '-', '-', '-', '-', ', '.join(map(str, notification_ids)) or '-')
    return (key, snapshot['episode'], snapshot['state'],
            'yes' if snapshot['open'] else 'no',
            'yes' if snapshot['acknowledged'] else 'no',
            'yes' if snapshot['silenced'] else 'no',
            'yes' if snapshot['notified'] else 'no',
            snapshot['silence_until'] or '-',
            ', '.join(map(str, notification_ids)) or '-')


INCIDENT_COLUMNS = ['key', 'episode', 'state', 'open', 'ack', 'silenced',
                    'notified', 'until', 'notification ids']


def notification_ids(incidents, key):
    return [item['notification_id'] for item in incidents.notification_records(key)]


def suite_faults():
    key = 'external_storage'
    svc = IncidentService()
    states = []

    svc.observe(key, 'o1', 1, 'FAULT')
    states.append(incident_row(key, svc.inspect(key), notification_ids(svc, key)))

    svc.ack(key)
    states.append(incident_row(key, svc.inspect(key), notification_ids(svc, key)))

    svc.observe(key, 'o2', 2, 'HEALTHY')
    states.append(incident_row(key, svc.inspect(key), notification_ids(svc, key)))

    svc.silence(key, 10)
    svc.observe(key, 'o3', 3, 'FAULT')
    states.append(incident_row(key, svc.inspect(key), notification_ids(svc, key)))

    svc.advance(9)
    states.append(incident_row(key, svc.inspect(key), notification_ids(svc, key)))

    svc.advance(10)
    states.append(incident_row(key, svc.inspect(key), notification_ids(svc, key)))

    svc.observe(key, 'o4', 4, 'FAULT')  # same OPEN episode: no second notice
    states.append(incident_row(key, svc.inspect(key), notification_ids(svc, key)))

    svc.observe(key, 'o5', 5, 'HEALTHY')
    svc.observe(key, 'o6', 6, 'FAULT')  # recovery then reappearance -> new id
    states.append(incident_row(key, svc.inspect(key), notification_ids(svc, key)))

    expected = [
        (key, 1, 'OPEN', 'yes', 'no', 'no', 'yes', '-', '1'),
        (key, 1, 'OPEN', 'yes', 'yes', 'no', 'yes', '-', '1'),
        (key, 1, 'RECOVERED', 'no', 'yes', 'no', 'yes', '-', '1'),
        (key, 2, 'OPEN', 'yes', 'no', 'yes', 'no', 10, '1'),
        (key, 2, 'OPEN', 'yes', 'no', 'yes', 'no', 10, '1'),
        (key, 2, 'OPEN', 'yes', 'no', 'no', 'yes', '-', '1, 2'),
        (key, 2, 'OPEN', 'yes', 'no', 'no', 'yes', '-', '1, 2'),
        (key, 3, 'OPEN', 'yes', 'no', 'no', 'yes', '-', '1, 2, 3'),
    ]
    assert states == expected, (states, expected)
    assert [item['episode'] for item in svc.notification_records(key)] == [1, 2, 3]

    # A late observation keeps evidence but changes no decision.
    late = IncidentService()
    late.observe('late_key', 'a', 1, 'FAULT')
    late.observe('late_key', 'c', 3, 'HEALTHY')
    result = late.observe('late_key', 'b', 2, 'FAULT')
    assert result['applied'] is False and len(late.episodes('late_key')) == 1
    assert [item['episode'] for item in late.notification_records('late_key')] == [1]

    # Illegal actions are rejected and leave no partial change.
    for action in (lambda: svc.ack('no_such_key'),
                   lambda: svc.silence(key, 0),
                   lambda: svc.advance(5),
                   lambda: svc.observe(key, 'o1', 1, 'HEALTHY'),
                   lambda: svc.observe(key, 'new', 6, 'FAULT')):
        try:
            action()
        except IncidentError:
            pass
        else:
            raise AssertionError('illegal incident action was accepted')

    show('external fault observations (key=%s)' % key, states, INCIDENT_COLUMNS)
    print('  late observation on late_key: applied=%s, episodes=%d, notices=%d'
          % (result['applied'], len(late.episodes('late_key')),
             len(late.notification_records('late_key'))))
    return states, expected


# --------------------------------------------------------------- suite 3
def suite_end_to_end():
    check = 'end_to_end_quality'
    expected_ids = [1, 2]
    good = EXAMPLES['good_rows']
    bad = [{'id': 1, 'value': None}, {'id': 2, 'value': 9}]
    quality = QualityRuns()
    incidents = IncidentService()
    bridge = QualityIncidentBridge(quality, incidents)
    rows = []

    def step(label):
        snap = incidents.inspect(check)
        current = quality.report(check)['current']
        rows.append((
            label,
            current['status'], current['observation'],
            'yes' if current['missing_ids'] else 'no',
            snap['episode'] if snap and snap['episode'] is not None else '-',
            snap['state'] if snap and snap['state'] is not None else '-',
            'yes' if snap and snap['acknowledged'] else 'no',
            'yes' if snap and snap['silenced'] else 'no',
            'yes' if snap and snap['notified'] else 'no',
            ', '.join(map(str, notification_ids(incidents, check))) or '-',
        ))

    bridge.process(check, 'r1', 1, bad, expected_ids, True)   # FAIL #1
    step('r1 fail')
    bridge.process(check, 'r2', 2, bad, expected_ids, True)   # FAIL #2 -> FAULT
    step('r2 fail')
    bridge.silence(check, 20)                                  # silence until tick 20
    step('silence')
    bridge.ack(check)                                          # ack inside silence
    step('ack')
    bridge.process(check, 'r2', 2, bad, expected_ids, True)   # duplicate delivery
    step('redeliver r2')
    bridge.process(check, 'r3', 3, good, expected_ids, True)  # PASS #1
    bridge.process(check, 'r4', 4, good, expected_ids, True)  # PASS #2 -> HEALTHY
    step('recover')
    bridge.process(check, 'r5', 5, bad, expected_ids, True)   # FAIL #1
    bridge.process(check, 'r6', 6, bad, expected_ids, True)   # FAIL #2 -> new FAULT
    step('recur in silence')
    bridge.advance(20)                                         # silence expires
    step('after expiry')

    expected = [
        ('r1 fail', 'FAIL', 'UNKNOWN', 'no', '-', '-', 'no', 'no', 'no', '-'),
        ('r2 fail', 'FAIL', 'FAULT', 'no', 1, 'OPEN', 'no', 'no', 'yes', '1'),
        ('silence', 'FAIL', 'FAULT', 'no', 1, 'OPEN', 'no', 'yes', 'yes', '1'),
        ('ack', 'FAIL', 'FAULT', 'no', 1, 'OPEN', 'yes', 'yes', 'yes', '1'),
        ('redeliver r2', 'FAIL', 'FAULT', 'no', 1, 'OPEN', 'yes', 'yes', 'yes', '1'),
        ('recover', 'PASS', 'HEALTHY', 'no', 1, 'RECOVERED', 'yes', 'yes', 'yes', '1'),
        ('recur in silence', 'FAIL', 'FAULT', 'no', 2, 'OPEN', 'no', 'yes', 'no', '1'),
        ('after expiry', 'FAIL', 'FAULT', 'no', 2, 'OPEN', 'no', 'no', 'yes', '1, 2'),
    ]
    assert rows == expected, (rows, expected)

    # Quality reports remained queryable and unchanged by operator activity.
    report = quality.report(check)
    assert report['current']['status'] == 'FAIL'
    assert [run['run_id'] for run in report['runs']] == ['r1', 'r2', 'r3', 'r4', 'r5', 'r6']
    assert len(incidents.episodes(check)) == 2
    assert [item['episode'] for item in incidents.notification_records(check)] == [1, 2]

    show('end-to-end along check=%s' % check, rows, [
        'step', 'quality', 'obs', 'missing', 'episode', 'state', 'ack',
        'silenced', 'notified', 'notif ids'])
    print('  history runs: %s' % [run['run_id'] for run in report['runs']])
    print('  episode identities: %s' % [ep['episode'] for ep in incidents.episodes(check)])
    print('  notification identities: %s'
          % [(item['episode'], item['notification_id'])
             for item in incidents.notification_records(check)])
    return rows, expected


def main():
    quality = suite_quality()
    faults = suite_faults()
    e2e = suite_end_to_end()
    print('\nAll expected state tables matched (%d + %d + %d steps).'
          % (len(quality[0]), len(faults[0]), len(e2e[0])))


if __name__ == '__main__':
    main()
