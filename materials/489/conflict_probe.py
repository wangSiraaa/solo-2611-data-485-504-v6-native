"""Actual SQLite virtual-table operations, compared with a native integer PK table."""
import json
from pathlib import Path
import tempfile
import apsw
from participant import TableTransactions
from virtual import CsvModule


def run(sql):
    expected = apsw.Connection(':memory:')
    expected.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, value INTEGER NOT NULL CHECK(value>=0))')
    expected.execute('INSERT INTO t VALUES(1,10),(2,20)')
    expected.execute('BEGIN')
    expected_error = None
    try:
        expected.execute(sql)
    except apsw.ConstraintError as error:
        expected_error = type(error).__name__
    expected_rows = list(expected.execute('SELECT id,value FROM t ORDER BY id'))
    expected_transaction_still_open = not expected.get_autocommit()
    expected.execute('ROLLBACK')
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'rows.csv'
        path.write_text('id,value\n1,10\n2,20\n')
        participant = TableTransactions(path)
        connection = apsw.Connection(':memory:')
        connection.create_module('csv_participant', CsvModule(participant), iVersion=2)
        connection.execute('CREATE VIRTUAL TABLE t USING csv_participant')
        connection.execute('BEGIN')
        actual_error = None
        try:
            connection.execute(sql)
        except Exception as error:
            actual_error = type(error).__name__ + ': ' + str(error)
        sql_rows = list(connection.execute('SELECT id,value FROM t ORDER BY id'))
        transaction_still_open = not connection.get_autocommit()
        physical_rows_before_rollback = participant.disk_rows()
        connection.execute('ROLLBACK')
        return {'sql': sql, 'actual': {'error': actual_error, 'sql_rows': sql_rows,
                    'transaction_still_open': transaction_still_open,
                    'file_before_rollback': physical_rows_before_rollback,
                    'file_after_rollback': participant.disk_rows(), 'events': participant.events},
                'native_sqlite_oracle': {'error': expected_error, 'rows': expected_rows,
                    'transaction_still_open': expected_transaction_still_open,
                    'file_during_and_after_rollback': {'1': 10, '2': 20}}}


if __name__ == '__main__':
    print(json.dumps([run(sql) for sql in [
        'INSERT INTO t VALUES(3,5)',
        'INSERT OR IGNORE INTO t VALUES(1,40),(3,5)',
        'INSERT OR REPLACE INTO t VALUES(1,40)',
        'UPDATE t SET id=3 WHERE id=1',
        'UPDATE OR IGNORE t SET id=2 WHERE id=1',
        'UPDATE OR REPLACE t SET id=2 WHERE id=1',
        'INSERT OR ABORT INTO t VALUES(3,5),(1,40)',
    ]], indent=2))
