"""Writable APSW virtual table adapter for the external CSV participant.

SQLite keeps doing what it is good at — SQL parsing, ordinary-table
constraints and statement/transaction bookkeeping — and only calls into
this module through the real version-2 virtual table callbacks (including
xSavepoint/xRollbackTo/xRelease).  Three details of the SQLite virtual
table contract shape the implementation:

1. SQLite does not enforce UNIQUE/PRIMARY KEY constraints on virtual tables
   (only NOT NULL is checked before a row reaches xUpdate).  Duplicate-id
   semantics are therefore implemented here, with the active conflict mode
   read from :meth:`apsw.Connection.vtab_on_conflict` so ABORT / IGNORE /
   REPLACE match a native ``id INTEGER PRIMARY KEY`` table row for row.

2. A virtual table joins the write transaction lazily, on its first
   xUpdate.  User SAVEPOINT levels opened before that moment never produce
   an xSavepoint callback; when xBegin finally runs SQLite only synthesizes
   one mark at the current nesting depth.  The adapter therefore keeps its
   own complete mark model — a user-savepoint stack plus, inside each user
   level, the statement-transaction snapshot — and uses the forwarded
   callbacks only to adjust it.

3. The SQLite wrapper ``sqlite3VtabSavepoint`` swallows xRollbackTo/xRelease
   calls for statement-transaction levels (real tables undo them through
   their pager journal).  A virtual table must undo its own xUpdate calls,
   which the adapter does by restoring the snapshot taken at the statement
   boundary whenever an xUpdate raises a constraint error.  Reliable
   statement-boundary signals come from the connection trace API
   (``SQLITE_TRACE_STMT`` before execution, ``SQLITE_TRACE_PROFILE`` when it
   finishes on either path), so even pure ``INSERT ... VALUES`` statements —
   which open no virtual-table cursor — are covered.
"""
import re

import apsw

# Leading command of a top-level SAVEPOINT statement.
_SAVEPOINT_RE = re.compile(r'^\s*SAVEPOINT\s+["`\[]?(\w+)', re.IGNORECASE)


class CsvModule:
    def __init__(self, participant):
        self.participant = participant

    def Create(self, connection, module_name, database_name, table_name):
        connection.vtab_config(apsw.SQLITE_VTAB_CONSTRAINT_SUPPORT, 1)
        table = CsvTable(self.participant, connection)
        trace_id = object()

        def trace(event):
            # Only top-level statements are relevant; nested virtual-table
            # queries are flagged as trigger activity.
            if event.get('trigger'):
                return
            sql = event['sql'] or ''
            if event['code'] == apsw.SQLITE_TRACE_STMT:
                # Fires before execution: a user SAVEPOINT is recognized here,
                # before its (possibly synthesized) xSavepoint callback.
                if _SAVEPOINT_RE.match(sql):
                    table._expect_user_savepoint = True
            elif event['code'] == apsw.SQLITE_TRACE_PROFILE:
                # Fires when the statement finishes, on both success and
                # error paths: the authoritative statement boundary.
                table._on_statement_done()

        connection.trace_v2(apsw.SQLITE_TRACE_STMT | apsw.SQLITE_TRACE_PROFILE,
                            trace, id=trace_id)
        table._remove_trace = lambda: connection.trace_v2(
            apsw.SQLITE_TRACE_STMT | apsw.SQLITE_TRACE_PROFILE, None,
            id=trace_id)
        return ('CREATE TABLE x(id INTEGER PRIMARY KEY, value INTEGER NOT NULL '
                'CHECK(value>=0))', table)

    Connect = Create


class CsvTable:
    def __init__(self, participant, connection):
        self.p = participant
        self.connection = connection
        # Mark model.  Each entry is one SQLite savepoint level:
        #   {'number': int, 'snapshot': dict, 'kind': 'user'|'stmt'}
        # Youngest last; every snapshot is an independent dict copy.
        self.marks = []
        # Whether the current writing statement's statement mark is open.
        # SQLite never forwards its rollback/release; the PROFILE trace event
        # is the authoritative end-of-statement signal.
        self._stmt_open = False
        # Set while a user SAVEPOINT command is in flight.  User savepoints
        # opened before the virtual table joins the transaction produce no
        # callback at all, so Begin() materializes the missing outer levels
        # from the depth reported by the synthesized xSavepoint.
        self._expect_user_savepoint = False
        # Installed by Create() to unregister the trace on disconnect.
        self._remove_trace = lambda: None

    # ----------------------------------------------------------- table plumbing
    def BestIndex(self, constraints, order_by):
        return None

    def Open(self):
        return Cursor(self)

    def Disconnect(self):
        # The trace is tied to the connection and vanishes with it on close;
        # unregister explicitly only while the connection is still alive.
        try:
            self._remove_trace()
        except apsw.ConnectionClosedError:
            pass

    Destroy = Disconnect

    # ------------------------------------------------- SQLite transaction hooks
    def Begin(self):
        self.p.begin()
        self.marks = []
        self._stmt_open = False

    def Sync(self):
        # Two-phase prepare phase: validate before any file write happens.
        # Map the participant's prepare rejection onto SQLITE_CONSTRAINT so
        # SQLite runs its normal commit-failure rollback path (xRollback).
        if self.p.active:
            try:
                self.p.prepare()
            except ValueError as exc:
                raise apsw.ConstraintError(str(exc))

    def Commit(self):
        if self.p.active:
            self.p.commit()
        self._reset()

    def Rollback(self):
        # Whole-transaction (or autocommit statement) rollback: the private
        # snapshot is discarded and the CSV was never touched.
        self.p.rollback()
        self._reset()

    def _reset(self):
        self.marks = []
        self._stmt_open = False
        self._expect_user_savepoint = False

    def Savepoint(self, number):
        if self._expect_user_savepoint:
            # Explicit user SAVEPOINT (flag set by the STMT trace callback).
            # Levels opened before the virtual table joined the transaction
            # never produced a callback, so materialize them first.
            self._backfill_user_levels(number)
            self.marks.append({'number': number, 'snapshot': self.p.rows(),
                               'kind': 'user'})
            self._expect_user_savepoint = False
            return
        # Statement-transaction level, announced lazily at the first write of
        # the statement.  Its rollback/release is never forwarded by SQLite,
        # so the adapter owns the complete lifecycle of this mark.
        self.marks.append({'number': number, 'snapshot': self.p.rows(),
                           'kind': 'stmt'})
        self._stmt_open = True

    def _backfill_user_levels(self, number):
        """Materialize user levels opened before the vtab joined the tx.

        The snapshots all equal the current private state, which for a
        late-joining table is still the committed contents: rolling back to
        any pre-join level must restore exactly that state.
        """
        have = {m['number'] for m in self.marks if m['kind'] == 'user'}
        for missing in range(number):
            if missing not in have:
                self.marks.append({'number': missing,
                                   'snapshot': self.p.rows(), 'kind': 'user'})

    def Release(self, number):
        # Forwarded user-level RELEASE (statement releases are swallowed):
        # invalidate this level and every younger level, keep the outer data.
        self._drop_from_number(number)

    def RollbackTo(self, number):
        # Forwarded user-level ROLLBACK TO: restore the saved state, keep the
        # target mark itself valid, discard every younger mark.
        for index in range(len(self.marks) - 1, -1, -1):
            if self.marks[index]['number'] == number and \
                    self.marks[index]['kind'] == 'user':
                self.p.private = dict(self.marks[index]['snapshot'])
                del self.marks[index + 1:]
                break

    def _drop_from_number(self, number):
        for index, mark in enumerate(self.marks):
            if mark['number'] == number:
                del self.marks[index:]
                return

    def _on_statement_done(self):
        """PROFILE callback: the current SQL statement has finished.

        A successful statement simply drops its private statement mark; an
        aborted one has already restored the boundary snapshot in
        :meth:`statement_aborted`.  User marks are never touched here.
        """
        if not self._stmt_open:
            return
        for index in range(len(self.marks) - 1, -1, -1):
            if self.marks[index]['kind'] == 'stmt':
                del self.marks[index:]
                break
        self._stmt_open = False

    def statement_aborted(self):
        """Undo the current statement after an xUpdate constraint failure."""
        if not self._stmt_open:
            return
        for index, mark in enumerate(self.marks):
            if mark['kind'] == 'stmt':
                # Restore the independent boundary snapshot and drop every
                # younger statement mark.
                self.p.private = dict(mark['snapshot'])
                del self.marks[index:]
                break
        self._stmt_open = False

    def _aborting_update(self, operation, *args):
        """Run an xUpdate mutation, undoing the statement on constraint error.

        The exception is re-raised unchanged so SQLite still reports the
        native-looking constraint error and keeps the outer transaction in
        its actual state; only the virtual table's private rows are restored.
        """
        try:
            return operation(*args)
        except apsw.ConstraintError:
            self.statement_aborted()
            raise

    # ------------------------------------------------------------- row updates
    def UpdateInsertRow(self, rowid, fields):
        return self._aborting_update(self._insert_row, rowid, fields)

    def _insert_row(self, rowid, fields):
        key, value = fields
        if key is None:
            key = rowid
        key, value = int(key), int(value)
        if value < 0:
            # Defensive mirror of the native table's CHECK(value>=0).
            raise apsw.ConstraintError('CHECK constraint failed: value')
        mode = self.connection.vtab_on_conflict()
        if key in self.p.rows():
            if mode == apsw.SQLITE_IGNORE:
                # Skip this row only; later rows of the statement still run.
                return apsw.SQLITE_IGNORE
            if mode == apsw.SQLITE_REPLACE:
                self.p.put(key, value)
                return key
            raise apsw.ConstraintError('UNIQUE constraint failed: x.id')
        self.p.put(key, value)
        return key

    def UpdateChangeRow(self, rowid, newrowid, fields):
        return self._aborting_update(self._change_row, rowid, newrowid, fields)

    def _change_row(self, rowid, newrowid, fields):
        key, value = int(fields[0]), int(fields[1])
        if value < 0:
            raise apsw.ConstraintError('CHECK constraint failed: value')
        rowid = int(rowid)
        if key != rowid and key in self.p.rows():
            mode = self.connection.vtab_on_conflict()
            if mode == apsw.SQLITE_IGNORE:
                return apsw.SQLITE_IGNORE
            if mode == apsw.SQLITE_REPLACE:
                # Native semantics: the moved row takes over the target id and
                # the displaced row disappears.
                self.p.remove(key)
            else:
                raise apsw.ConstraintError('UNIQUE constraint failed: x.id')
        # Identity move: remove the old rowid and install under the new id,
        # carrying the (possibly changed) value with it.
        self.p.remove(rowid)
        self.p.put(key, value)

    def UpdateDeleteRow(self, rowid):
        return self._aborting_update(self._delete_row, rowid)

    def _delete_row(self, rowid):
        self.p.remove(int(rowid))


class Cursor:
    def __init__(self, table):
        self.table = table
        self.p = table.p
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
