"""Local owned rows, with a quota participant injected by the storage client."""
import json


class WarehouseRows:
    def __init__(self, rights):
        self.rights = rights
        self.db = rights.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS warehouse_rows(
                id TEXT PRIMARY KEY,value INTEGER NOT NULL CHECK(value>=0),usage TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS row_receipts(
                operation_id TEXT PRIMARY KEY,request TEXT NOT NULL);
        ''')

    def import_rows(self, operation_id, rows):
        request = json.dumps({'kind': 'import', 'rows': rows}, sort_keys=True)
        old = self.db.execute('SELECT request FROM row_receipts WHERE operation_id=?',
                              (operation_id,)).fetchone()
        if old:
            if old[0] != request:
                raise ValueError('operation identity conflict')
            return
        for index, row in enumerate(rows):
            self.rights.spend(operation_id + ':' + str(index), 1)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for index, row in enumerate(rows):
                self.db.execute('INSERT INTO warehouse_rows VALUES(?,?,?)',
                                (row['id'], row['value'], operation_id + ':' + str(index)))
            self.db.execute('INSERT INTO row_receipts VALUES(?,?)', (operation_id, request))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def delete(self, operation_id, identities):
        request = json.dumps({'kind': 'delete', 'ids': identities}, sort_keys=True)
        old = self.db.execute('SELECT request FROM row_receipts WHERE operation_id=?',
                              (operation_id,)).fetchone()
        if old:
            if old[0] != request:
                raise ValueError('operation identity conflict')
            return
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for identity in identities:
                row = self.db.execute('SELECT usage FROM warehouse_rows WHERE id=?', (identity,)).fetchone()
                if row:
                    self.rights.restore(row[0])
                    self.db.execute('DELETE FROM warehouse_rows WHERE id=?', (identity,))
            self.db.execute('INSERT INTO row_receipts VALUES(?,?)', (operation_id, request))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def rows(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM warehouse_rows ORDER BY id')]
