"""Local owned rows, with a quota participant injected by the storage client.

Rows live in the same SQLite database as the local ``Rights`` instance, so the
quota debit, the row writes and the operation receipt share one local
transaction.  ``Rights`` is used only through ``spend``/``restore`` so a test
can substitute an accurate local quota participant that follows the same
transaction contract.
"""
import json

try:  # works both as a flat module and as part of a namespace package
    from rights import canonical
except ImportError:  # pragma: no cover - package-style import
    from .rights import canonical


_INT64_MAX = (1 << 63) - 1


def _is_text(value):
    return isinstance(value, str) and value != ''


def _valid_value(value):
    return type(value) is int and 0 <= value <= _INT64_MAX


def _valid_row(row):
    return (
        isinstance(row, dict)
        and set(row.keys()) == {'id', 'value'}
        and _is_text(row['id'])
        and _valid_value(row['value'])
    )


class WarehouseRows:
    def __init__(self, rights):
        self.rights = rights
        self.db = rights.db
        # One live row owns exactly one unreturned one-unit usage.  The
        # row_usages ledger records the ownership for audit; warehouse_rows
        # itself references the usage directly.
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS warehouse_rows(
                id TEXT PRIMARY KEY CHECK(length(id) > 0),
                value INTEGER NOT NULL
                    CHECK(typeof(value) = 'integer' AND value >= 0),
                usage TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS row_receipts(
                operation_id TEXT PRIMARY KEY CHECK(length(operation_id) > 0),
                request TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS row_usages(
                usage TEXT PRIMARY KEY,
                row_id TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0, 1))
            );
        ''')

    # -- helpers ----------------------------------------------------------
    def _usage_for(self, operation_id, index):
        # An operation id is unique per kind in the shared namespace, so this
        # usage id is unique as well.  A later operation with a fresh id uses
        # a fresh usage; a restored usage is never reused.
        return 'row:' + operation_id + ':' + str(index)

    def _existing_receipt(self, operation_id):
        return self.db.execute(
            'SELECT request FROM row_receipts WHERE operation_id=?',
            (operation_id,),
        ).fetchone()

    def _check_replay(self, operation_id, request):
        """Return True when this exact operation already committed."""
        old = self._existing_receipt(operation_id)
        if old is None:
            return False
        if old['request'] != request:
            raise ValueError('operation identity conflict')
        return True

    # -- operations -------------------------------------------------------
    def import_rows(self, operation_id, rows):
        if not _is_text(operation_id):
            raise ValueError('invalid operation id')
        if not isinstance(rows, list):
            raise ValueError('invalid rows')
        seen = set()
        for row in rows:
            if not _valid_row(row):
                raise ValueError('invalid row')
            if row['id'] in seen:
                raise ValueError('duplicate row id in batch')
            seen.add(row['id'])
        # Note: capacity adequacy is deliberately checked *inside* the
        # transaction, after the replay check.  A successful import retry must
        # stay idempotent even if the local balance has since dropped.
        request = canonical({'kind': 'import', 'rows': rows})

        owns = not self.db.in_transaction
        if owns:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            if self._check_replay(operation_id, request):
                if owns:
                    self.db.commit()
                return None

            # A local id that already exists makes the whole batch fail.
            for row in rows:
                if self.db.execute(
                    'SELECT 1 FROM warehouse_rows WHERE id=?', (row['id'],)
                ).fetchone():
                    raise ValueError('row id already exists')

            # Consume one unreturned unit of rights per row.  Since the
            # enclosing transaction is already open these participate in it.
            for index, row in enumerate(rows):
                self.rights.spend(self._usage_for(operation_id, index), 1)

            for index, row in enumerate(rows):
                usage = self._usage_for(operation_id, index)
                self.db.execute(
                    'INSERT INTO warehouse_rows(id, value, usage) '
                    'VALUES(?, ?, ?)',
                    (row['id'], row['value'], usage),
                )
                self.db.execute(
                    'INSERT INTO row_usages(usage, row_id, active) '
                    'VALUES(?, ?, 1)',
                    (usage, row['id']),
                )

            self.db.execute(
                'INSERT INTO row_receipts(operation_id, request) VALUES(?, ?)',
                (operation_id, request),
            )
            if owns:
                self.db.commit()
            return None
        except Exception:
            if owns:
                self.db.rollback()
            raise

    def delete(self, operation_id, identities):
        if not _is_text(operation_id):
            raise ValueError('invalid operation id')
        if not isinstance(identities, list):
            raise ValueError('invalid identities')
        for identity in identities:
            if not _is_text(identity):
                raise ValueError('invalid identity')

        request = canonical({'kind': 'delete', 'identities': identities})

        owns = not self.db.in_transaction
        if owns:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            if self._check_replay(operation_id, request):
                if owns:
                    self.db.commit()
                return None

            for identity in identities:
                row = self.db.execute(
                    'SELECT usage FROM warehouse_rows WHERE id=?',
                    (identity,),
                ).fetchone()
                if row is None:
                    # Deleting an absent id has no effect.  A repeated id in
                    # the list therefore cannot return the same usage twice.
                    continue
                # restore() itself is idempotent per usage, so even a usage
                # that somehow was already returned cannot add capacity.
                self.rights.restore(row['usage'])
                self.db.execute(
                    'UPDATE row_usages SET active=0 WHERE usage=?',
                    (row['usage'],),
                )
                self.db.execute(
                    'DELETE FROM warehouse_rows WHERE id=?', (identity,)
                )

            self.db.execute(
                'INSERT INTO row_receipts(operation_id, request) VALUES(?, ?)',
                (operation_id, request),
            )
            if owns:
                self.db.commit()
            return None
        except Exception:
            if owns:
                self.db.rollback()
            raise

    # -- reads ------------------------------------------------------------
    def rows(self):
        return [
            dict(row)
            for row in self.db.execute(
                'SELECT id, value, usage FROM warehouse_rows ORDER BY id'
            )
        ]

    def row_usages(self):
        return [
            dict(row)
            for row in self.db.execute(
                'SELECT usage, row_id, active FROM row_usages ORDER BY usage'
            )
        ]

    def receipts(self):
        return [
            {'operation_id': row['operation_id'], 'request': json.loads(row['request'])}
            for row in self.db.execute(
                'SELECT operation_id, request FROM row_receipts '
                'ORDER BY operation_id'
            )
        ]
