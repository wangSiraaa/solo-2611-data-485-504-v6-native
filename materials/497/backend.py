"""Support fixture: real SQLite transactions plus explicit simulated driver faults.
SQLSTATE labels here are fixture protocol labels, not SQLite-native diagnostics.
"""
import sqlite3

class DriverFault(Exception):
    def __init__(self, message, sqlstate, phase, *, transaction_aborted=False, commit_unknown=False):
        super().__init__(message)
        self.sqlstate=sqlstate;self.phase=phase
        self.transaction_aborted=transaction_aborted;self.commit_unknown=commit_unknown

class Backend:
    def __init__(self, path, faults=None):
        self.db=sqlite3.connect(path);self.faults=list(faults or [])
        self.active=False;self.aborted=False;self.events=[]

    def begin(self):
        self.db.execute('BEGIN');self.active=True;self.aborted=False;self.events.append('BEGIN')

    def execute(self, sql, params=()):
        if self.aborted:raise DriverFault('transaction aborted','25P02','execute',transaction_aborted=True)
        self.events.append('EXECUTE '+sql)
        if self.faults and self.faults[0]=='serialization' and sql.startswith('INSERT'):
            self.faults.pop(0);self.aborted=True
            raise DriverFault('serialization failure','40001','execute',transaction_aborted=True)
        return self.db.execute(sql,params).fetchall()

    def commit(self):
        if self.aborted:raise DriverFault('cannot commit failed transaction','25P02','commit',transaction_aborted=True)
        self.db.commit();self.active=False;self.events.append('COMMIT_ACTUALLY_DONE')
        if self.faults and self.faults[0]=='commit_lost':
            self.faults.pop(0)
            raise DriverFault('commit acknowledgement lost','08006','commit',commit_unknown=True)

    def rollback(self):
        self.db.rollback();self.active=False;self.aborted=False;self.events.append('ROLLBACK')

    def close(self):self.db.close()
