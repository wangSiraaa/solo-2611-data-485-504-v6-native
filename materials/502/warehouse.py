"""Warehouse fact changes and their outgoing change envelopes."""
import json
import sqlite3


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def connect(path):
    db = sqlite3.connect(path, isolation_level=None, timeout=1, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS facts(
            key TEXT PRIMARY KEY, value INTEGER NOT NULL CHECK(value>=0), version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS receipts(
            operation_id TEXT PRIMARY KEY, request TEXT NOT NULL, event_ids TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(
            event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', owner TEXT,
            generation INTEGER NOT NULL DEFAULT 0, lease_until INTEGER);
        CREATE TABLE IF NOT EXISTS attempts(
            event_id TEXT NOT NULL, generation INTEGER NOT NULL, owner TEXT NOT NULL,
            state TEXT NOT NULL, PRIMARY KEY(event_id,generation));
    ''')
    return db


class Warehouse:
    def __init__(self, path):
        self.db = connect(path)
        self.before_event = None

    def apply(self, operation_id, rows):
        request = canonical(rows)
        old_receipt = self.db.execute('SELECT * FROM receipts WHERE operation_id=?',
                                      (operation_id,)).fetchone()
        if old_receipt:
            if old_receipt['request'] != request:
                raise ValueError('operation identity conflict')
            return json.loads(old_receipt['event_ids'])
        events = []
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for index, item in enumerate(rows):
                if (not isinstance(item['key'], str) or not item['key']
                        or type(item['value']) is not int):
                    raise ValueError('invalid fact row')
                old = self.db.execute('SELECT value,version FROM facts WHERE key=?',
                                      (item['key'],)).fetchone()
                if old and old['value'] == item['value']:
                    continue
                version = old['version'] + 1 if old else 1
                self.db.execute('INSERT INTO facts VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET '
                                'value=excluded.value,version=excluded.version',
                                (item['key'], item['value'], version))
                body = dict(operation_id=operation_id, source_no=index,
                            key=item['key'], old=old['value'] if old else None,
                            new=item['value'], version=version)
                events.append((operation_id + ':' + str(index), canonical(body)))
            self.db.execute('INSERT INTO receipts VALUES(?,?,?)',
                            (operation_id, request, canonical([event_id for event_id, _ in events])))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        for event_id, payload in events:
            if self.before_event is not None:
                self.before_event(event_id)
            self.db.execute('INSERT INTO outbox(event_id,payload) VALUES(?,?)',
                            (event_id, payload))
        return [event_id for event_id, _ in events]

    def inspect(self):
        return {table: [dict(row) for row in self.db.execute('SELECT * FROM ' + table)]
                for table in ('facts', 'receipts', 'outbox', 'attempts')}
