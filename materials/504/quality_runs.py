"""Batch data-quality products, also queried without an incident manager."""
from copy import deepcopy


class QualityRuns:
    def __init__(self):
        self.runs = {}
        self.current = {}

    def ingest(self, check, run_id, sequence, rows, expected_ids, complete=True):
        bad = [row['id'] for row in rows if row['value'] is None]
        present = {row['id'] for row in rows}
        missing = sorted(set(expected_ids) - present)
        status = 'FAIL' if bad else 'PASS'
        previous = self.current.get(check, {'fail_streak': 0, 'pass_streak': 0})
        result = {'check': check, 'run_id': run_id, 'sequence': sequence,
                  'status': status, 'observed_rows': len(rows),
                  'bad_row_ids': sorted(bad), 'missing_ids': missing,
                  'complete': complete,
                  'fail_streak': previous['fail_streak'] + 1 if status == 'FAIL' else 0,
                  'pass_streak': previous['pass_streak'] + 1 if status == 'PASS' else 0}
        result['observation'] = ('FAULT' if result['fail_streak'] >= 2 else
                                 'HEALTHY' if result['pass_streak'] >= 2 else 'UNKNOWN')
        self.runs[(check, run_id)] = deepcopy(result)
        self.current[check] = deepcopy(result)
        return deepcopy(result)

    def report(self, check):
        return {'current': deepcopy(self.current.get(check)),
                'runs': [deepcopy(row) for (name, _), row in self.runs.items() if name == check]}
