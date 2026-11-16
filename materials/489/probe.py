"""Observe ordinary and independent contract failures in temporary copies."""
import copy
import json
from pathlib import Path
import tempfile
import apsw
from participant import TableTransactions
from virtual import CsvModule


class SnapshotParticipant(TableTransactions):
    """Probe adapter isolates virtual-table behavior from participant aliasing."""
    def savepoint(self, number):
        self.marks[number] = copy.deepcopy(self.private)
        self.events.append(['savepoint', number])


def setup(directory, participant=TableTransactions):
    path = directory / 'data.csv'
    path.write_text('id,value\n1,10\n2,20\n')
    p = participant(path)
    db = apsw.Connection(str(directory / 'work.sqlite'))
    db.create_module('csv_external', CsvModule(p), iVersion=2)
    db.execute('CREATE VIRTUAL TABLE ext USING csv_external')
    db.execute('CREATE TABLE ordinary(id INTEGER PRIMARY KEY, marker INTEGER CHECK(marker>=0))')
    db.execute('INSERT INTO ordinary VALUES(1,1)')
    return path, p, db


def run():
    results = {}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for name in ('normal', 'participant_normal', 'participant', 'virtual', 'prepare'):
            folder = root / name
            folder.mkdir()
            path, p, db = setup(folder, SnapshotParticipant if name in ('virtual', 'prepare') else TableTransactions)
            try:
                if name == 'normal':
                    db.execute('BEGIN; UPDATE ext SET value=15 WHERE id=1; COMMIT')
                    results[name] = {'actual': p.disk_rows(), 'expected': {1: 15, 2: 20}}
                elif name == 'participant_normal':
                    p.begin()
                    p.put(1, 15)
                    p.prepare()
                    p.commit()
                    results[name] = {'actual': p.disk_rows(), 'expected': {1: 15, 2: 20}}
                elif name == 'participant':
                    p.begin()
                    p.savepoint(0)
                    p.put(1, 40)
                    p.rollback_to(0)
                    p.release(0)
                    p.savepoint(0)
                    p.put(2, 25)
                    p.rollback_to(0)
                    p.prepare()
                    p.commit()
                    results[name] = {'actual': p.disk_rows(), 'expected': {1: 10, 2: 20}}
                elif name == 'virtual':
                    db.execute('BEGIN; UPDATE ext SET value=40 WHERE id=1')
                    error = None
                    try:
                        db.execute('UPDATE ordinary SET marker=-1 WHERE id=1')
                    except apsw.ConstraintError as exc:
                        error = type(exc).__name__
                        db.execute('ROLLBACK')
                    results[name] = {'sql_error': error, 'actual': p.disk_rows(),
                                     'expected': {1: 10, 2: 20},
                                     'ordinary': list(db.execute('SELECT * FROM ordinary'))}
                else:
                    db.execute('BEGIN; UPDATE ordinary SET marker=2; UPDATE ext SET value=90 WHERE id=1')
                    error = None
                    try:
                        db.execute('COMMIT')
                    except Exception as exc:
                        error = str(exc)
                        if not db.get_autocommit():
                            db.execute('ROLLBACK')
                    results[name] = {'prepare_error': error, 'actual': p.disk_rows(),
                                     'expected': {1: 10, 2: 20},
                                     'ordinary': list(db.execute('SELECT * FROM ordinary'))}
                results[name]['events'] = p.events
                results[name]['matches_expected'] = results[name]['actual'] == results[name]['expected']
            finally:
                db.close()
    return results


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
