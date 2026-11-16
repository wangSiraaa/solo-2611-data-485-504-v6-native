"""Prototype conditional data service, backed by actual SQLite writes."""
import sqlite3


class StoreError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=3)
        self.db.execute('CREATE TABLE IF NOT EXISTS records('
                        'key TEXT PRIMARY KEY,value INTEGER NOT NULL CHECK(value>=0),revision INTEGER NOT NULL)')
        self.after_check = None

    def read(self, key):
        row = self.db.execute('SELECT key,value,revision FROM records WHERE key=?', (key,)).fetchone()
        if row is None:
            raise StoreError('NOT_FOUND')
        return dict(key=row[0], value=row[1], etag='v:' + str(row[2]))

    def create(self, key, value):
        try:
            self.db.execute('INSERT INTO records VALUES(?,?,1)', (key, value))
        except sqlite3.IntegrityError as exc:
            raise StoreError('CONSTRAINT') from exc
        return self.read(key)

    def _condition(self, key, if_match):
        if if_match is None:
            raise StoreError('PRECONDITION_REQUIRED')
        row = self.read(key)
        if row['etag'] != if_match:
            raise StoreError('PRECONDITION_FAILED')
        if self.after_check is not None:
            self.after_check()

    def update(self, key, value, if_match):
        self._condition(key, if_match)
        try:
            self.db.execute('UPDATE records SET value=?,revision=revision+1 WHERE key=?', (value, key))
        except sqlite3.IntegrityError as exc:
            raise StoreError('CONSTRAINT') from exc
        return self.read(key)

    def delete(self, key, if_match):
        self._condition(key, if_match)
        self.db.execute('DELETE FROM records WHERE key=?', (key,))
        return dict(key=key, deleted=True)

    def close(self):
        self.db.close()
