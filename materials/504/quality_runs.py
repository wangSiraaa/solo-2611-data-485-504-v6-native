"""Batch data-quality evidence, also usable without an incident manager.

Identity and content
--------------------
A run is identified by ``(check, run_id)``.  Its canonical content is the
six-field record ``check, run_id, sequence, rows, expected_ids, complete``.
JSON object key order is irrelevant, while the order of the ``rows`` and
``expected_ids`` arrays is significant.  Re-sending the same identity with the
same canonical content returns the originally stored result and changes no
counter; re-sending it with different content is rejected and the original
record is kept.

Per-run status (precedence, in order)
-------------------------------------
1. any observed row with ``value is None``      -> ``FAIL``
2. otherwise ``complete is False``              -> ``UNKNOWN``
3. otherwise missing expected row ids           -> ``FAIL``
4. otherwise (complete coverage, no NULL)       -> ``PASS``

An empty ``expected_ids`` with ``complete=True`` is therefore a legitimate,
fully-covered empty batch (``PASS``); an empty batch with ``complete=False``
is ``UNKNOWN``.  The two must not be conflated.

Frontier and consecutive evidence
---------------------------------
Only a run whose ``sequence`` is greater than the check's current frontier may
update the current quality result and the consecutive evidence.  A smaller,
late run keeps its own row/status evidence in the history but never rewrites
the current result and never retroactively changes a fault decision.

Consecutive evidence counts adjacent ``sequence`` values belonging to
distinct runs.  A sequence gap or an ``UNKNOWN`` status breaks a PASS/FAIL
streak.  Two consecutive ``FAIL`` runs yield ``observation='FAULT'``, two
consecutive ``PASS`` runs yield ``HEALTHY``, and anything else yields
``UNKNOWN``.  This derived observation never changes a run's own status.

All query results are deep copies; callers cannot mutate internal state.
"""
from copy import deepcopy
import json


class QualityInputError(ValueError):
    """Invalid batch input or run-identity conflict; nothing is changed."""


_CONTENT_FIELDS = ('check', 'run_id', 'sequence', 'rows', 'expected_ids', 'complete')


def canonical_text(value):
    """Deterministic JSON text: object keys sorted, list order preserved."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _is_int(value):
    return type(value) is int  # reject bool, which is an int subclass


def _validate(check, run_id, sequence, rows, expected_ids, complete):
    if not isinstance(check, str) or not check:
        raise QualityInputError('check must be a non-empty string')
    if not isinstance(run_id, str) or not run_id:
        raise QualityInputError('run_id must be a non-empty string')
    if not _is_int(sequence) or sequence <= 0:
        raise QualityInputError('sequence must be a positive integer')
    if not isinstance(complete, bool):
        raise QualityInputError('complete must be a boolean')
    if not isinstance(expected_ids, (list, tuple)):
        raise QualityInputError('expected_ids must be a list')
    if not isinstance(rows, (list, tuple)):
        raise QualityInputError('rows must be a list')

    declared = set()
    for item in expected_ids:
        if not _is_int(item):
            raise QualityInputError('expected_ids must contain integers')
        if item in declared:
            raise QualityInputError('expected_ids must not repeat an id')
        declared.add(item)

    present = set()
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise QualityInputError('each row must be an object')
        if set(row) != {'id', 'value'}:
            raise QualityInputError('each row must contain exactly id and value')
        row_id = row['id']
        if not _is_int(row_id):
            raise QualityInputError('row id must be an integer')
        if row_id not in declared:
            raise QualityInputError('row id %r is not declared in expected_ids' % (row_id,))
        if row_id in present:
            raise QualityInputError('row ids must be unique')
        present.add(row_id)
        value = row['value']
        if value is not None and not _is_int(value):
            raise QualityInputError('row value must be an integer or null')
        normalized.append({'id': row_id, 'value': value})

    return {
        'check': check,
        'run_id': run_id,
        'sequence': sequence,
        'rows': normalized,
        'expected_ids': list(expected_ids),
        'complete': complete,
    }


class QualityRuns:
    def __init__(self):
        self._runs = {}          # (check, run_id) -> stored result
        self._content = {}       # (check, run_id) -> canonical content text
        self._checks = {}        # check -> frontier/current/history

    # ------------------------------------------------------------------ ingest
    def ingest(self, check, run_id, sequence, rows, expected_ids, complete=True):
        """Ingest one batch and return its immutable per-run evidence."""
        content = _validate(check, run_id, sequence, rows, expected_ids, complete)
        identity = (check, run_id)
        canonical = canonical_text(content)

        stored_content = self._content.get(identity)
        if stored_content is not None:
            if stored_content != canonical:
                raise QualityInputError(
                    'run identity conflict for check=%r run_id=%r' % (check, run_id))
            return deepcopy(self._runs[identity])

        check_state = self._checks.get(check)
        if check_state is None:
            check_state = {'frontier': 0, 'current': None, 'history': {}}

        occupant = check_state['history'].get(sequence)
        if occupant is not None and occupant['run_id'] != run_id:
            raise QualityInputError(
                'sequence %d is already used by run %r in check %r'
                % (sequence, occupant['run_id'], check))

        # No state is touched before every rejection path above is passed.
        result = self._evaluate(content)
        if sequence > check_state['frontier']:
            result['advances_frontier'] = True
            result['fail_streak'], result['pass_streak'] = self._streaks(
                check_state, sequence, result['status'])
            result['observation'] = (
                'FAULT' if result['fail_streak'] >= 2
                else 'HEALTHY' if result['pass_streak'] >= 2
                else 'UNKNOWN')
            check_state['frontier'] = sequence
            check_state['current'] = deepcopy(result)
        else:
            # Late run: retain its own evidence only; it is not on the
            # current consecutive chain, so it contributes no streak.
            result['advances_frontier'] = False
            result['fail_streak'] = 0
            result['pass_streak'] = 0
            result['observation'] = 'UNKNOWN'

        self._content[identity] = canonical
        self._runs[identity] = deepcopy(result)
        check_state['history'][sequence] = deepcopy(result)
        self._checks[check] = check_state
        return deepcopy(result)

    @staticmethod
    def _evaluate(content):
        rows = content['rows']
        bad = [row['id'] for row in rows if row['value'] is None]
        present = {row['id'] for row in rows}
        missing = sorted(set(content['expected_ids']) - present)
        if bad:
            status = 'FAIL'
        elif not content['complete']:
            status = 'UNKNOWN'
        elif missing:
            status = 'FAIL'
        else:
            status = 'PASS'
        return {
            'check': content['check'],
            'run_id': content['run_id'],
            'sequence': content['sequence'],
            'status': status,
            'observed_rows': len(rows),
            'bad_row_ids': sorted(bad),
            'missing_ids': missing,
            'complete': content['complete'],
            'expected_ids': list(content['expected_ids']),
            'rows': deepcopy(rows),
        }

    @staticmethod
    def _streaks(check_state, sequence, status):
        if status == 'UNKNOWN':
            return 0, 0
        previous = check_state['history'].get(sequence - 1)
        if previous is None or previous['status'] != status:
            return (1, 0) if status == 'FAIL' else (0, 1)
        if status == 'FAIL':
            return previous['fail_streak'] + 1, 0
        return 0, previous['pass_streak'] + 1

    # ----------------------------------------------------------------- queries
    def report(self, check):
        """Return the current result and the full history for one check."""
        state = self._checks.get(check)
        if state is None:
            return {'current': None, 'runs': []}
        return {
            'current': deepcopy(state['current']),
            'runs': [deepcopy(state['history'][sequence])
                     for sequence in sorted(state['history'])],
        }

    def run(self, check, run_id):
        """Return one stored run's evidence, or None."""
        stored = self._runs.get((check, run_id))
        return None if stored is None else deepcopy(stored)

    # Compatibility read-only views; each call returns fresh copies.
    @property
    def runs(self):
        return {identity: deepcopy(result) for identity, result in self._runs.items()}

    @property
    def current(self):
        return {check: deepcopy(state['current'])
                for check, state in self._checks.items() if state['current'] is not None}
