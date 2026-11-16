"""Writable APSW virtual table adapter for the external transaction API."""
import apsw


class CsvModule:
    def __init__(self, participant):
        self.participant = participant

    def Create(self, connection, module_name, database_name, table_name):
        connection.vtab_config(apsw.SQLITE_VTAB_CONSTRAINT_SUPPORT, 1)
        return 'CREATE TABLE x(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)', CsvTable(self.participant, connection)

    Connect = Create


class CsvTable:
    def __init__(self, participant, connection):
        self.p = participant
        self.connection = connection

    def BestIndex(self, constraints, order_by):
        return None

    def Open(self):
        return Cursor(self.p)

    def Disconnect(self):
        pass

    Destroy = Disconnect

    def Begin(self):
        self.p.begin()

    def Sync(self):
        if self.p.active:
            self.p.prepare()

    def Commit(self):
        if self.p.active:
            self.p.commit()

    def Rollback(self):
        self.p.rollback()

    def Savepoint(self, number):
        self.p.savepoint(number)

    def Release(self, number):
        self.p.release(number)

    def RollbackTo(self, number):
        self.p.rollback_to(number)

    def UpdateInsertRow(self, rowid, fields):
        key, value = fields
        key = rowid if key is None else key
        if key is None:
            key = max(self.p.rows(), default=0) + 1
        if key in self.p.rows():
            raise ValueError('duplicate id')
        self.p.put(key, value)
        self.p.publish_preview()
        return key

    def UpdateChangeRow(self, rowid, newrowid, fields):
        key, value = fields
        if key != rowid or newrowid != rowid:
            raise ValueError('id changes unsupported')
        self.p.put(key, value)
        self.p.publish_preview()

    def UpdateDeleteRow(self, rowid):
        self.p.remove(rowid)
        self.p.publish_preview()


class Cursor:
    def __init__(self, participant):
        self.p = participant
        self.data = []
        self.index = 0

    def Filter(self, indexnum, indexname, values):
        self.data = sorted(self.p.rows().items())
        self.index = 0

    def Eof(self):
        return self.index >= len(self.data)

    def Rowid(self):
        return self.data[self.index][0]

    def Column(self, index):
        return self.data[self.index][index]

    def Next(self):
        self.index += 1

    def Close(self):
        pass
