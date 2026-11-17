"""Escrow capacity rights; usable independently of the row warehouse.

A site holds a local integer balance.  Spending consumes positive units and
creates an unreturned ``usage`` record.  A transfer atomically debits local
available units and persists an outgoing message; the destination only credits
the message when it is explicitly received.  Duplicate identities are handled
by content-addressed ledgers, so retries, reordering and replays never create
capacity.
"""
import json
import sqlite3


def canonical(value):
    """Canonical JSON: object key order is ignored, array order is kept."""
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False
    )


def _is_text(value):
    return isinstance(value, str) and value != ''


def _is_positive_int(value):
    # bool is a subclass of int, but True/False are not positive units.
    return type(value) is int and value > 0


_MESSAGE_KEYS = {'id', 'source', 'destination', 'units'}


class Rights:
    def __init__(self, path, site, initial_grant):
        if site not in ('A', 'B'):
            raise ValueError('invalid site')
        # Grants are non-negative and their sum K is at most 20, so a single
        # site can never legitimately hold more than 20.
        if type(initial_grant) is not int or not 0 <= initial_grant <= 20:
            raise ValueError('invalid initial grant')
        self.site = site
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS rights_meta(
                site TEXT PRIMARY KEY,
                initial_grant INTEGER NOT NULL,
                available INTEGER NOT NULL,
                outgoing_units INTEGER NOT NULL DEFAULT 0,
                received_units INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS outgoing(
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS received(
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usages(
                id TEXT PRIMARY KEY,
                units INTEGER NOT NULL CHECK(units > 0),
                returned INTEGER NOT NULL CHECK(returned IN (0, 1))
            );
        ''')
        # Initialization is itself one local transaction: either the first
        # grant is issued exactly once, or the existing configuration is
        # accepted unchanged.  A different site or grant is rejected.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM rights_meta').fetchone()
            if row is None:
                self.db.execute(
                    'INSERT INTO rights_meta(site, initial_grant, available) '
                    'VALUES(?, ?, ?)',
                    (site, initial_grant, initial_grant),
                )
            elif row['site'] != site or row['initial_grant'] != initial_grant:
                raise ValueError('incompatible existing site')
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    # -- local capacity ---------------------------------------------------
    def available(self):
        return self.db.execute('SELECT available FROM rights_meta').fetchone()[0]

    def spend(self, usage, units=1):
        if not _is_text(usage):
            raise ValueError('invalid usage identity')
        if not _is_positive_int(units):
            raise ValueError('invalid consumption')
        owns = not self.db.in_transaction
        if owns:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute(
                'SELECT * FROM usages WHERE id=?', (usage,)
            ).fetchone()
            if old is not None:
                # Same usage and same units is an idempotent retry.  A
                # returned usage is finished and may not be spent again.
                if old['units'] != units or old['returned']:
                    raise ValueError('usage identity conflict')
            else:
                if units > self.available():
                    raise ValueError('insufficient local rights')
                self.db.execute(
                    'UPDATE rights_meta SET available=available-?', (units,)
                )
                self.db.execute(
                    'INSERT INTO usages(id, units, returned) VALUES(?, ?, 0)',
                    (usage, units),
                )
            if owns:
                self.db.commit()
            return usage
        except Exception:
            if owns:
                self.db.rollback()
            raise

    def restore(self, usage):
        if not _is_text(usage):
            raise ValueError('invalid usage identity')
        owns = not self.db.in_transaction
        if owns:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute(
                'SELECT * FROM usages WHERE id=?', (usage,)
            ).fetchone()
            if row is None:
                raise ValueError('unknown usage')
            if not row['returned']:
                self.db.execute(
                    'UPDATE rights_meta SET available=available+?',
                    (row['units'],),
                )
                self.db.execute(
                    'UPDATE usages SET returned=1 WHERE id=?', (usage,)
                )
            if owns:
                self.db.commit()
            return None
        except Exception:
            if owns:
                self.db.rollback()
            raise

    # -- transfers --------------------------------------------------------
    def transfer(self, identity, destination, units):
        if not _is_text(identity):
            raise ValueError('invalid transfer identity')
        if destination not in ('A', 'B') or destination == self.site:
            raise ValueError('invalid transfer destination')
        if not _is_positive_int(units):
            raise ValueError('invalid transfer units')

        # The source prefix makes the id globally unique: an identity is only
        # unique within a site, and A/B prefixes cannot collide.
        message = {
            'id': self.site + ':' + identity,
            'source': self.site,
            'destination': destination,
            'units': units,
        }
        payload = canonical(message)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute(
                'SELECT payload FROM outgoing WHERE id=?', (message['id'],)
            ).fetchone()
            if old is not None:
                if old['payload'] != payload:
                    raise ValueError('transfer identity conflict')
                # Idempotent retry: return the originally persisted message
                # and do not debit again.
                self.db.commit()
                return json.loads(old['payload'])
            if units > self.available():
                raise ValueError('insufficient local rights')
            self.db.execute(
                'UPDATE rights_meta SET available=available-?, '
                'outgoing_units=outgoing_units+?',
                (units, units),
            )
            self.db.execute(
                'INSERT INTO outgoing(id, payload) VALUES(?, ?)',
                (message['id'], payload),
            )
            self.db.commit()
            return message
        except Exception:
            self.db.rollback()
            raise

    def receive(self, message):
        if not isinstance(message, dict):
            raise ValueError('invalid message')
        if set(message.keys()) != _MESSAGE_KEYS:
            raise ValueError('invalid message fields')
        if not _is_text(message['id']):
            raise ValueError('invalid message id')
        if message['source'] not in ('A', 'B'):
            raise ValueError('invalid source')
        if message['destination'] not in ('A', 'B'):
            raise ValueError('invalid destination')
        if message['source'] == message['destination']:
            raise ValueError('source and destination are equal')
        if message['destination'] != self.site:
            raise ValueError('message is not addressed to this site')
        # Honest transfers mint ids as ``source + ':' + identity`` so the id
        # space cannot collide across sites; the receiver only insists on a
        # non-empty text id, since message content is otherwise canonical.
        if not _is_positive_int(message['units']):
            raise ValueError('invalid message units')

        payload = canonical(message)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute(
                'SELECT payload FROM received WHERE id=?', (message['id'],)
            ).fetchone()
            if old is not None:
                if old['payload'] != payload:
                    raise ValueError('received message identity conflict')
                # Duplicate delivery of identical content: already credited.
                self.db.commit()
                return message
            # These ledgers are independent of the source-side ledger; a
            # message that was accepted here is no longer in flight even if
            # the source has not observed anything.
            self.db.execute(
                'UPDATE rights_meta SET available=available+?, '
                'received_units=received_units+?',
                (message['units'], message['units']),
            )
            self.db.execute(
                'INSERT INTO received(id, payload) VALUES(?, ?)',
                (message['id'], payload),
            )
            self.db.commit()
            return message
        except Exception:
            self.db.rollback()
            raise

    # -- evidence ---------------------------------------------------------
    def inspect(self):
        meta = self.db.execute('SELECT * FROM rights_meta').fetchone()
        return {
            'site': self.site,
            'initial_grant': meta['initial_grant'],
            'available': meta['available'],
            'outgoing_units': meta['outgoing_units'],
            'received_units': meta['received_units'],
            'outgoing': [
                json.loads(row['payload'])
                for row in self.db.execute(
                    'SELECT payload FROM outgoing ORDER BY id'
                )
            ],
            'received': [
                row['id']
                for row in self.db.execute('SELECT id FROM received ORDER BY id')
            ],
            'received_messages': [
                json.loads(row['payload'])
                for row in self.db.execute(
                    'SELECT payload FROM received ORDER BY id'
                )
            ],
            'usages': [
                dict(row)
                for row in self.db.execute('SELECT * FROM usages ORDER BY id')
            ],
        }

    def close(self):
        self.db.close()
