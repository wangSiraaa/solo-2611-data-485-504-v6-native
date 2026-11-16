"""SQLite-backed conditional record store.

Records are a text key, a non-negative int64 value and an opaque,
service-generated etag.  An etag identifies exactly one version of one row
lifetime:

* every committed create/update mints a fresh, globally monotonic version id
  (the ``meta.next_ver`` counter, incremented inside the write transaction);
* an etag matches only while the row exists and its current version id is the
  one encoded in the token, so a successful update (even with an unchanged
  value), a delete/re-create cycle or closing and reopening the database can
  never make an old committed etag valid again;
* rolling back a transaction also rolls the counter back, but that version id
  was never attached to any committed row, so no committed etag is affected.

Conditional writes are one atomic decision: a direct call takes a write lock
with ``BEGIN IMMEDIATE`` and performs the compare and the write with a single
conditional SQL statement (``... WHERE key=? AND ver=?``); it never reads in
one transaction and then writes unconditionally in another.  The row and etag
returned are read back inside that same transaction.

Callers that want to span several operations (the batch layer) open a
:class:`Txn` via :meth:`Store.begin` and pass it in; the store then joins the
caller's transaction and never commits or rolls it back on its own.
"""
import secrets
import sqlite3
import threading

MAX_KEY_CODEPOINTS = 64
MAX_VALUE = (1 << 63) - 1  # non-negative int64 upper bound


class StoreError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Txn:
    """A write transaction owned by the caller.

    Store methods accept a ``Txn`` and run inside it without issuing their own
    BEGIN/COMMIT/ROLLBACK, so a batch can keep a whole group's prefix private
    until it decides to commit or roll the group back.
    """

    def __init__(self, db, lock):
        self.db = db
        self._lock = lock
        self._closed = False

    def commit(self):
        if not self._closed:
            try:
                self.db.execute('COMMIT')
            finally:
                self._closed = True
                self._lock.release()

    def rollback(self):
        if not self._closed:
            try:
                self.db.execute('ROLLBACK')
            finally:
                self._closed = True
                self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class Store:
    def __init__(self, path):
        # isolation_level=None: we manage every transaction boundary
        # explicitly, which is what makes transaction participation possible.
        self.db = sqlite3.connect(str(path), isolation_level=None,
                                  check_same_thread=False, timeout=30)
        self.db.execute('PRAGMA busy_timeout=30000')
        # One Store serializes whole operations on its connection; real
        # concurrent clients use separate Store instances on the same file,
        # where SQLite's write lock provides the cross-process/connection
        # mutual exclusion.
        self._lock = threading.RLock()
        self._init_schema()
        row = self.db.execute(
            "SELECT value FROM props WHERE name='db_id'").fetchone()
        if row is None:
            # Random instance id makes tokens from different database files
            # unmatchable; the token format itself is opaque to callers.
            self.db_id = secrets.token_hex(8)
            self.db.execute("INSERT INTO props(name, value) VALUES('db_id', ?)",
                            (self.db_id,))
            self.db.execute("INSERT INTO meta(name, value) VALUES('next_ver', 1)")
        else:
            self.db_id = row[0]

    def _init_schema(self):
        self.db.execute(
            'CREATE TABLE IF NOT EXISTS records('
            'key TEXT PRIMARY KEY,'
            'value INTEGER NOT NULL CHECK(value BETWEEN 0 AND 9223372036854775807),'
            'ver INTEGER NOT NULL)')
        self.db.execute(
            'CREATE TABLE IF NOT EXISTS meta('
            'name TEXT PRIMARY KEY, value INTEGER NOT NULL)')
        self.db.execute(
            'CREATE TABLE IF NOT EXISTS props('
            'name TEXT PRIMARY KEY, value TEXT NOT NULL)')

    # ------------------------------------------------------------------ etag

    def _make_etag(self, ver):
        return 'W/"%s-%d"' % (self.db_id, ver)

    def _parse_etag(self, token):
        """Return the encoded version id, or None if the token cannot match."""
        if not isinstance(token, str):
            return None
        prefix = 'W/"%s-' % self.db_id
        if not (token.startswith(prefix) and token.endswith('"')):
            return None
        body = token[len(prefix):-1]
        if not body.isdigit():
            return None
        return int(body)

    def _next_ver(self, conn):
        # Incremented inside the caller's transaction, so rollback restores it.
        row = conn.execute(
            "UPDATE meta SET value=value+1 WHERE name='next_ver' RETURNING value"
        ).fetchone()
        return row[0]

    def _read_row(self, conn, key):
        row = conn.execute(
            'SELECT key, value, ver FROM records WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        return {'key': row[0], 'value': row[1], 'etag': self._make_etag(row[2])}

    # -------------------------------------------------------------- validation

    @staticmethod
    def _check_key(key):
        # bool is not accepted as a key (it is not text) and code points,
        # not UTF-16 units or bytes, are measured.
        if not isinstance(key, str) or not (1 <= len(key) <= MAX_KEY_CODEPOINTS):
            raise StoreError('BAD_REQUEST')

    @staticmethod
    def _check_value(value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise StoreError('BAD_REQUEST')
        if not (0 <= value <= MAX_VALUE):
            raise StoreError('BAD_REQUEST')

    @staticmethod
    def _require_if_match(if_match):
        if if_match is None:
            raise StoreError('PRECONDITION_REQUIRED')
        if not isinstance(if_match, str):
            raise StoreError('BAD_REQUEST')

    # --------------------------------------------------------- transaction API

    def begin(self):
        """Open an immediate write transaction and return its owner handle.

        The connection lock is held until commit/rollback, so no other thread
        on this Store can interleave statements inside the caller's group.
        """
        self._lock.acquire()
        try:
            self.db.execute('BEGIN IMMEDIATE')
        except BaseException:
            self._lock.release()
            raise
        return Txn(self.db, self._lock)

    def transaction(self):
        """Context-managed write transaction (commits on success)."""
        return self.begin()

    def _conn(self, txn):
        return self.db if txn is None else txn.db

    def _run_write(self, txn, work):
        """Run ``work(conn)`` atomically.

        With no caller txn, take the connection lock and own the full
        BEGIN IMMEDIATE / COMMIT / ROLLBACK cycle, so a raised condition
        error always leaves the connection back in autocommit.  With a
        caller txn (batch group), join it without touching its boundaries.
        """
        if txn is not None:
            return work(txn.db)
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                result = work(self.db)
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            self.db.execute('COMMIT')
            return result

    # ----------------------------------------------------------------- reads

    def read(self, key, txn=None):
        self._check_key(key)
        conn = self._conn(txn)
        if txn is None:
            with self._lock:
                result = self._read_row(conn, key)
        else:
            result = self._read_row(conn, key)
        if result is None:
            raise StoreError('NOT_FOUND')
        return result

    # ----------------------------------------------------------------- writes

    def create(self, key, value, txn=None):
        self._check_key(key)
        self._check_value(value)

        def work(conn):
            ver = self._next_ver(conn)
            try:
                conn.execute(
                    'INSERT INTO records(key, value, ver) VALUES(?, ?, ?)',
                    (key, value, ver))
            except sqlite3.IntegrityError as exc:
                raise StoreError('CONSTRAINT') from exc
            return self._read_row(conn, key)

        return self._run_write(txn, work)

    def update(self, key, value, if_match, txn=None):
        self._check_key(key)
        self._check_value(value)
        self._require_if_match(if_match)
        expected_ver = self._parse_etag(if_match)

        def work(conn):
            row = conn.execute(
                'SELECT ver FROM records WHERE key=?', (key,)).fetchone()
            if row is None:
                raise StoreError('NOT_FOUND')
            if row[0] != expected_ver:
                raise StoreError('PRECONDITION_FAILED')
            new_ver = self._next_ver(conn)
            # Compare and write in one conditional statement while the write
            # lock is held: no other connection can change the row in between.
            cursor = conn.execute(
                'UPDATE records SET value=?, ver=? WHERE key=? AND ver=?',
                (value, new_ver, key, expected_ver))
            if cursor.rowcount != 1:
                # Defensive: cannot happen while we hold the lock.
                raise StoreError('PRECONDITION_FAILED')
            return self._read_row(conn, key)

        return self._run_write(txn, work)

    def delete(self, key, if_match, txn=None):
        self._check_key(key)
        self._require_if_match(if_match)
        expected_ver = self._parse_etag(if_match)

        def work(conn):
            row = conn.execute(
                'SELECT ver FROM records WHERE key=?', (key,)).fetchone()
            if row is None:
                raise StoreError('NOT_FOUND')
            if row[0] != expected_ver:
                raise StoreError('PRECONDITION_FAILED')
            cursor = conn.execute(
                'DELETE FROM records WHERE key=? AND ver=?',
                (key, expected_ver))
            if cursor.rowcount != 1:
                raise StoreError('PRECONDITION_FAILED')
            return {'key': key, 'deleted': True}

        return self._run_write(txn, work)

    def close(self):
        self.db.close()
