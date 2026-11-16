"""Escrow capacity rights; usable independently of the row warehouse."""
import json
import sqlite3


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class Rights:
    def __init__(self, path, site, initial_grant):
        if site not in ('A', 'B') or type(initial_grant) is not int or initial_grant < 0:
            raise ValueError('invalid initial grant')
        self.site = site
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS rights_meta(site TEXT PRIMARY KEY, initial_grant INTEGER, available INTEGER);
            CREATE TABLE IF NOT EXISTS outgoing(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS received(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usages(id TEXT PRIMARY KEY, units INTEGER NOT NULL, returned INTEGER NOT NULL);
        ''')
        row = self.db.execute('SELECT * FROM rights_meta').fetchone()
        if row is None:
            self.db.execute('INSERT INTO rights_meta VALUES(?,?,?)', (site, initial_grant, initial_grant))
        elif row['site'] != site or row['initial_grant'] != initial_grant:
            raise ValueError('incompatible existing site')

    def available(self):
        return self.db.execute('SELECT available FROM rights_meta').fetchone()[0]

    def transfer(self, identity, destination, units):
        if destination not in ('A', 'B') or destination == self.site or type(units) is not int or units <= 0:
            raise ValueError('invalid transfer')
        message = dict(id=self.site + ':' + identity, source=self.site,
                       destination=destination, units=units)
        payload = canonical(message)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT payload FROM outgoing WHERE id=?', (message['id'],)).fetchone()
            if old:
                if old['payload'] != payload:
                    raise ValueError('transfer identity conflict')
            else:
                if units > self.available():
                    raise ValueError('insufficient local rights')
                self.db.execute('UPDATE rights_meta SET available=available-?', (units,))
                self.db.execute('INSERT INTO outgoing VALUES(?,?)', (message['id'], payload))
            self.db.commit()
            return message
        except Exception:
            self.db.rollback()
            raise

    def receive(self, message):
        if (message['destination'] != self.site or message['source'] == self.site
                or type(message['units']) is not int or message['units'] <= 0):
            raise ValueError('invalid destination or units')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE rights_meta SET available=available+?', (message['units'],))
            self.db.execute('INSERT OR IGNORE INTO received VALUES(?,?)',
                            (message['id'], canonical(message)))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def spend(self, usage, units=1):
        if type(units) is not int or units <= 0:
            raise ValueError('invalid consumption')
        owns = not self.db.in_transaction
        if owns:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT * FROM usages WHERE id=?', (usage,)).fetchone()
            if old:
                if old['units'] != units or old['returned']:
                    raise ValueError('usage identity conflict')
            else:
                if units > self.available():
                    raise ValueError('insufficient local rights')
                self.db.execute('UPDATE rights_meta SET available=available-?', (units,))
                self.db.execute('INSERT INTO usages VALUES(?,?,0)', (usage, units))
            if owns:
                self.db.commit()
            return usage
        except Exception:
            if owns:
                self.db.rollback()
            raise

    def restore(self, usage):
        row = self.db.execute('SELECT * FROM usages WHERE id=?', (usage,)).fetchone()
        if row is None:
            raise ValueError('unknown usage')
        self.db.execute('UPDATE rights_meta SET available=available+?', (row['units'],))
        self.db.execute('UPDATE usages SET returned=1 WHERE id=?', (usage,))

    def inspect(self):
        return {'site': self.site, 'available': self.available(),
                'outgoing': [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM outgoing')],
                'received': [row[0] for row in self.db.execute('SELECT id FROM received')],
                'usages': [dict(row) for row in self.db.execute('SELECT * FROM usages')]}
