"""Real SQLite migration executor and graph-aware upgrade entry.

The executor never fabricates graph dependencies for a direct ``execute`` call:
it only runs the operation record it is given.  ``upgrade`` is the bridge that
walks a :class:`revision_graph.RevisionGraph` plan and records success into the
graph after a revision is *fully* committed.

Execution follows SQLite's real transaction boundaries:

* consecutive ``sql`` operations form one transaction;
* before a ``vacuum`` operation the current SQL segment is committed and the
  VACUUM runs outside any transaction;
* SQL after a VACUUM starts a new transaction.

Metadata lives in ``_migration_*`` tables in the same database.  It is written
in its own short transactions, never inside a revision's SQL segment, so it
survives a later operation failure.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

MAX_OPERATIONS = 4
METADATA_PREFIX = '_migration_'

# A single operation must not carry transaction control or VACUUM; the executor
# owns those boundaries.  Everything else is passed to SQLite unchanged.
_FORBIDDEN_FIRST_WORDS = {
    'BEGIN', 'COMMIT', 'END', 'ROLLBACK', 'SAVEPOINT', 'RELEASE', 'VACUUM',
}


class MigrationExecutorError(Exception):
    """Base class for executor errors."""


class InvalidOperation(ValueError, MigrationExecutorError):
    """An operation record is malformed or outside the supported contract."""


class RevisionDefinitionConflict(ValueError, MigrationExecutorError):
    """The same revision id was reused with a different operation definition."""

    def __init__(self, message, *, revision=None, stored_digest=None,
                 requested_digest=None, existing_runs=None):
        super().__init__(message)
        self.revision = revision
        self.stored_digest = stored_digest
        self.requested_digest = requested_digest
        self.existing_runs = list(existing_runs or [])


class ExecutorStateError(RuntimeError, MigrationExecutorError):
    """The connection or durable run state cannot be used safely."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _split_sql(sql):
    """Split SQL text on semicolons outside quotes and comments."""
    statements = []
    buffer = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in "'\"`[":
            closing = ']' if char == '[' else char
            buffer.append(char)
            index += 1
            while index < length:
                current = sql[index]
                buffer.append(current)
                index += 1
                if current == closing:
                    # doubled quote/backtick is an escaped character
                    if closing in "'\"`" and index < length and sql[index] == closing:
                        buffer.append(sql[index])
                        index += 1
                        continue
                    break
        elif char == '-' and index + 1 < length and sql[index + 1] == '-':
            buffer.append(' ')
            index += 2
            while index < length and sql[index] != '\n':
                index += 1
            if index < length:
                buffer.append('\n')
                index += 1
        elif char == '/' and index + 1 < length and sql[index + 1] == '*':
            buffer.append(' ')
            index += 2
            while index + 1 < length and not (sql[index] == '*' and sql[index + 1] == '/'):
                index += 1
            index = min(length, index + 2)
        elif char == ';':
            statement = ''.join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
        else:
            buffer.append(char)
            index += 1
    statement = ''.join(buffer).strip()
    if statement:
        statements.append(statement)
    return statements


def _strip_quoted(sql):
    """Replace quoted strings/comments with spaces, keeping identifiers."""
    out = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in "'\"`[":
            closing = ']' if char == '[' else char
            index += 1
            while index < length:
                current = sql[index]
                index += 1
                if current == closing:
                    if closing in "'\"`" and index < length and sql[index] == closing:
                        index += 1
                        continue
                    break
            out.append(' ')
        elif char == '-' and index + 1 < length and sql[index + 1] == '-':
            index += 2
            while index < length and sql[index] != '\n':
                index += 1
            out.append(' ')
        elif char == '/' and index + 1 < length and sql[index + 1] == '*':
            index += 2
            while index + 1 < length and not (sql[index] == '*' and sql[index + 1] == '/'):
                index += 1
            index = min(length, index + 2)
            out.append(' ')
        else:
            out.append(char)
            index += 1
    return ''.join(out)


def _references_metadata(sql):
    """True when the (unquoted) statement names an executor metadata table."""
    return re.search(r'\b' + re.escape(METADATA_PREFIX) + r'\w*', _strip_quoted(sql)) is not None


def _normalize_operations(operations):
    """Validate and canonicalize an operation record."""
    if isinstance(operations, (str, bytes)):
        raise InvalidOperation('operations must be a sequence, not text')
    try:
        operations = list(operations)
    except TypeError:
        raise InvalidOperation('operations must be a sequence') from None
    if len(operations) > MAX_OPERATIONS:
        raise InvalidOperation(f'at most {MAX_OPERATIONS} operations are supported')
    normalized = []
    for position, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise InvalidOperation(f'operation {position} must be a mapping')
        kind = operation.get('kind')
        if kind == 'sql':
            extra = set(operation) - {'kind', 'sql'}
            if extra:
                raise InvalidOperation(
                    f'operation {position} has unknown keys: {sorted(extra)}')
            sql = operation.get('sql')
            if not isinstance(sql, str) or not sql.strip():
                raise InvalidOperation(f'operation {position} needs a non-empty sql string')
            statements = _split_sql(sql)
            if len(statements) != 1:
                raise InvalidOperation(
                    f'operation {position} must contain exactly one SQL statement, '
                    f'found {len(statements)}')
            statement = statements[0]
            first = statement.lstrip().split(None, 1)[0].upper()
            if first in _FORBIDDEN_FIRST_WORDS:
                raise InvalidOperation(
                    f'operation {position} must not use {first}; the executor owns '
                    'transactions and VACUUM')
            if _references_metadata(sql):
                raise InvalidOperation(
                    f'operation {position} must not access executor metadata tables')
            normalized.append({'kind': 'sql', 'sql': sql})
        elif kind == 'vacuum':
            extra = set(operation) - {'kind'}
            if extra:
                raise InvalidOperation(
                    f'operation {position} has unknown keys: {sorted(extra)}')
            normalized.append({'kind': 'vacuum'})
        else:
            raise InvalidOperation(
                f"operation {position} has unknown kind {kind!r}; expected 'sql' or 'vacuum'")
    return normalized


def _definition_text(operations):
    return _json(operations)


def _definition_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _decode(value, default):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class MigrationExecutor:
    """Execute operation segments and keep durable, queryable run records."""

    def __init__(self, connection):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError('connection must be a sqlite3.Connection')
        self.connection = connection
        if connection.in_transaction:
            raise ExecutorStateError(
                'connection has an open transaction; commit or roll back before '
                'constructing MigrationExecutor')
        # The executor owns transaction boundaries; rely on explicit BEGIN/COMMIT.
        if connection.isolation_level is not None:
            connection.isolation_level = None
        self._ensure_schema()

    # -- schema ------------------------------------------------------------
    def _ensure_schema(self):
        self.connection.executescript(f'''
            CREATE TABLE IF NOT EXISTS {METADATA_PREFIX}definitions(
                revision_id TEXT PRIMARY KEY,
                definition_digest TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                bound_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS {METADATA_PREFIX}runs(
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                revision_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                state TEXT NOT NULL,
                definition_digest TEXT NOT NULL,
                completed_ops TEXT NOT NULL DEFAULT '[]',
                committed_sql_segments TEXT NOT NULL DEFAULT '[]',
                external_ops TEXT NOT NULL DEFAULT '[]',
                rolled_back_ops TEXT NOT NULL DEFAULT '[]',
                rolled_back_sql_segments TEXT NOT NULL DEFAULT '[]',
                uncertain_segments TEXT NOT NULL DEFAULT '[]',
                failed_op INTEGER,
                failed_phase TEXT,
                error TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                UNIQUE(revision_id, attempt)
            );
            CREATE INDEX IF NOT EXISTS {METADATA_PREFIX}runs_revision
                ON {METADATA_PREFIX}runs(revision_id);
            CREATE TABLE IF NOT EXISTS {METADATA_PREFIX}complete(
                revision_id TEXT PRIMARY KEY,
                run_id INTEGER NOT NULL,
                definition_digest TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS {METADATA_PREFIX}heads(
                revision_id TEXT PRIMARY KEY,
                recorded_at TEXT NOT NULL
            );
        ''')

    # -- metadata transactions --------------------------------------------
    def _begin_metadata(self):
        if self.connection.in_transaction:
            raise ExecutorStateError('metadata write attempted inside an open transaction')
        self.connection.execute('BEGIN IMMEDIATE')

    def _commit_metadata(self):
        self.connection.execute('COMMIT')

    def _rollback_metadata(self):
        if self.connection.in_transaction:
            self.connection.execute('ROLLBACK')

    # -- durable lookups ---------------------------------------------------
    def _definition_row(self, revision):
        return self.connection.execute(
            f'SELECT definition_digest,definition_json,bound_at '
            f'FROM {METADATA_PREFIX}definitions WHERE revision_id=?',
            (revision,)).fetchone()

    def run_records(self, revision=None):
        """Return durable run records, oldest attempt first."""
        if revision is None:
            rows = self.connection.execute(
                f'SELECT * FROM {METADATA_PREFIX}runs ORDER BY revision_id,attempt').fetchall()
        else:
            rows = self.connection.execute(
                f'SELECT * FROM {METADATA_PREFIX}runs WHERE revision_id=? ORDER BY attempt',
                (revision,)).fetchall()
        columns = [description[0] for description in self.connection.execute(
            f'SELECT * FROM {METADATA_PREFIX}runs LIMIT 0').description]
        records = []
        for row in rows:
            record = dict(zip(columns, row))
            for key in ('completed_ops', 'committed_sql_segments', 'external_ops',
                        'rolled_back_ops', 'rolled_back_sql_segments',
                        'uncertain_segments'):
                record[key] = _decode(record.get(key), [])
            records.append(record)
        return records

    def complete_revisions(self):
        return [row[0] for row in self.connection.execute(
            f'SELECT revision_id FROM {METADATA_PREFIX}complete ORDER BY revision_id')]

    def stored_heads(self):
        """Heads last persisted by a graph-aware upgrade (inspection/load)."""
        return [row[0] for row in self.connection.execute(
            f'SELECT revision_id FROM {METADATA_PREFIX}heads ORDER BY revision_id')]

    # -- receipt construction ---------------------------------------------
    @staticmethod
    def _evidence(record):
        return {
            'run_id': record['run_id'],
            'attempt': record['attempt'],
            'state': record['state'],
            'definition_digest': record['definition_digest'],
            'completed_operations': record['completed_ops'],
            'committed_sql_segments': record['committed_sql_segments'],
            'external_operations': record['external_ops'],
            'rolled_back_operations': record['rolled_back_ops'],
            'rolled_back_sql_segments': record['rolled_back_sql_segments'],
            'uncertain_segments': record['uncertain_segments'],
            'failed_operation': record['failed_op'],
            'failed_phase': record['failed_phase'],
            'error': record['error'],
            'started_at': record['started_at'],
            'ended_at': record['ended_at'],
        }

    def _blocked_receipt(self, revision, digest, reason, message, records):
        return {
            'revision': revision,
            'state': 'blocked',
            'reason': reason,
            'message': message,
            'definition_digest': digest,
            'executed': False,
            'existing_runs': [self._evidence(record) for record in records],
        }

    # -- preflight ---------------------------------------------------------
    def _preflight(self, revision, digest, retry):
        definition = self._definition_row(revision)
        records = self.run_records(revision)
        if definition is not None and definition[0] != digest:
            raise RevisionDefinitionConflict(
                f'revision {revision!r} is already bound to operation definition '
                f'{definition[0]}, not the requested {digest}; changing the '
                'definition requires a new revision id',
                revision=revision, stored_digest=definition[0],
                requested_digest=digest,
                existing_runs=[self._evidence(record) for record in records])
        if records and definition is None:
            raise ExecutorStateError(
                f'revision {revision!r} has run records but no bound definition')
        for record in records:
            if record['state'] in ('running', 'partial'):
                return self._blocked_receipt(
                    revision, digest, 'partial_or_incomplete_attempt',
                    (f'revision {revision!r} has a previous {record["state"]} '
                     'attempt; automatic replay is blocked, inspect the evidence '
                     'and continue with a new revision id/manual repair'),
                    records)
        for record in records:
            if record['state'] == 'complete':
                return {
                    'revision': revision,
                    'state': 'complete',
                    'already_complete': True,
                    'noop': True,
                    'executed': False,
                    'run_id': record['run_id'],
                    'attempt': record['attempt'],
                    'definition_digest': digest,
                    'completed_operations': record['completed_ops'],
                    'committed_sql_segments': record['committed_sql_segments'],
                    'external_operations': record['external_ops'],
                    'rolled_back_operations': [],
                    'rolled_back_sql_segments': [],
                    'uncertain_segments': [],
                    'failed_operation': None,
                    'failed_phase': None,
                    'error': None,
                    'existing_runs': [self._evidence(item) for item in records],
                }
        if records and records[-1]['state'] == 'failed' and not retry:
            return self._blocked_receipt(
                revision, digest, 'previous_failure_requires_explicit_retry',
                (f'revision {revision!r} previously failed with no committed '
                 'effects; pass retry=True to start a new attempt with the same '
                 'definition'),
                records)
        return None

    def _start_run(self, revision, digest, definition_text):
        self._begin_metadata()
        try:
            definition = self._definition_row(revision)
            if definition is not None and definition[0] != digest:
                raise RevisionDefinitionConflict(
                    f'revision {revision!r} is already bound to a different '
                    'operation definition', revision=revision,
                    stored_digest=definition[0], requested_digest=digest)
            if definition is None:
                self.connection.execute(
                    f'INSERT INTO {METADATA_PREFIX}definitions '
                    '(revision_id,definition_digest,definition_json,bound_at) '
                    'VALUES(?,?,?,?)',
                    (revision, digest, definition_text, _now()))
            attempt = self.connection.execute(
                f'SELECT COALESCE(MAX(attempt),0)+1 FROM {METADATA_PREFIX}runs '
                'WHERE revision_id=?', (revision,)).fetchone()[0]
            cursor = self.connection.execute(
                f'INSERT INTO {METADATA_PREFIX}runs '
                '(revision_id,attempt,state,definition_digest,started_at) '
                "VALUES(?,?,'running',?,?)",
                (revision, attempt, digest, _now()))
            run_id = cursor.lastrowid
            self._commit_metadata()
            return run_id, attempt
        except Exception:
            self._rollback_metadata()
            raise

    # -- real operation execution -----------------------------------------
    def _receipt(self, revision, digest, run_id, attempt, **fields):
        receipt = {
            'revision': revision,
            'state': fields.pop('state'),
            'already_complete': False,
            'executed': True,
            'run_id': run_id,
            'attempt': attempt,
            'definition_digest': digest,
            'completed_operations': [],
            'committed_sql_segments': [],
            'external_operations': [],
            'rolled_back_operations': [],
            'rolled_back_sql_segments': [],
            'uncertain_segments': [],
            'failed_operation': None,
            'failed_phase': None,
            'error': None,
        }
        receipt.update(fields)
        return receipt

    def _finish_run(self, run_id, receipt):
        state = receipt['state']
        # A lost COMMIT result can leave the driver's transaction state open.
        # Never start a metadata transaction on top of it.
        if self.connection.in_transaction:
            try:
                self.connection.execute('ROLLBACK')
            except Exception as exc:
                raise ExecutorStateError(
                    'cannot close the open transaction before recording run '
                    f'evidence: {exc}') from exc
        self._begin_metadata()
        try:
            self.connection.execute(
                f'UPDATE {METADATA_PREFIX}runs SET '
                'state=?,completed_ops=?,committed_sql_segments=?,external_ops=?,'
                'rolled_back_ops=?,rolled_back_sql_segments=?,uncertain_segments=?,'
                'failed_op=?,failed_phase=?,error=?,ended_at=? WHERE run_id=?',
                (state,
                 _json(receipt['completed_operations']),
                 _json(receipt['committed_sql_segments']),
                 _json(receipt['external_operations']),
                 _json(receipt['rolled_back_operations']),
                 _json(receipt['rolled_back_sql_segments']),
                 _json(receipt['uncertain_segments']),
                 receipt['failed_operation'],
                 receipt['failed_phase'],
                 receipt['error'],
                 _now(),
                 run_id))
            if state == 'complete':
                self.connection.execute(
                    f'INSERT INTO {METADATA_PREFIX}complete '
                    '(revision_id,run_id,definition_digest,completed_at) '
                    'VALUES(?,?,?,?) ON CONFLICT(revision_id) DO UPDATE SET '
                    'run_id=excluded.run_id,definition_digest=excluded.definition_digest,'
                    'completed_at=excluded.completed_at',
                    (receipt['revision'], run_id, receipt['definition_digest'], _now()))
            self._commit_metadata()
        except Exception:
            self._rollback_metadata()
            raise

    def _execute_operations(self, revision, operations, digest, run_id, attempt):
        committed_sql_segments = []
        external_operations = []
        completed = []                # only operations that really committed/completed
        current = []                  # indices in the open SQL segment
        rolled_back_ops = []
        rolled_back_sql_segments = []
        uncertain_segments = []
        failure = None

        for index, operation in enumerate(operations):
            if operation['kind'] == 'sql':
                try:
                    if not self.connection.in_transaction:
                        self.connection.execute('BEGIN')
                    self.connection.execute(operation['sql'])
                except Exception as exc:
                    # The failing statement never completed; its whole open
                    # segment is rolled back and reported separately.
                    failure = {'op': index, 'phase': 'sql', 'error': exc}
                    break
                current.append(index)
                continue

            # -- vacuum: commit the preceding SQL segment first --
            if current:
                try:
                    self.connection.execute('COMMIT')
                    committed_sql_segments.append({'operations': list(current)})
                    completed.extend(current)
                    current = []
                except Exception as exc:
                    if self.connection.in_transaction:
                        try:
                            self.connection.execute('ROLLBACK')
                            rolled_back_ops.extend(current)
                            rolled_back_sql_segments.append({'operations': list(current)})
                        except Exception as rollback_exc:
                            uncertain_segments.append({
                                'operations': list(current), 'phase': 'commit',
                                'error': str(rollback_exc)})
                        current = []
                        failure = {'op': index, 'phase': 'commit', 'error': exc}
                    else:
                        # The driver lost the commit result.  Do not claim it.
                        uncertain_segments.append({
                            'operations': list(current), 'phase': 'commit',
                            'error': str(exc)})
                        current = []
                        failure = {'op': index, 'phase': 'commit', 'error': exc}
                    break
            try:
                if self.connection.in_transaction:
                    raise ExecutorStateError('VACUUM cannot run inside a transaction')
                self.connection.execute('VACUUM')
            except Exception as exc:
                failure = {'op': index, 'phase': 'vacuum', 'error': exc}
                break
            external_operations.append(index)
            completed.append(index)
        else:
            # All operations ran; commit the trailing SQL segment.
            if current:
                try:
                    self.connection.execute('COMMIT')
                    committed_sql_segments.append({'operations': list(current)})
                    completed.extend(current)
                    current = []
                except Exception as exc:
                    if self.connection.in_transaction:
                        try:
                            self.connection.execute('ROLLBACK')
                            rolled_back_ops.extend(current)
                            rolled_back_sql_segments.append({'operations': list(current)})
                        except Exception as rollback_exc:
                            uncertain_segments.append({
                                'operations': list(current), 'phase': 'commit',
                                'error': str(rollback_exc)})
                        current = []
                        failure = {'op': None, 'phase': 'commit', 'error': exc}
                    else:
                        uncertain_segments.append({
                            'operations': list(current), 'phase': 'commit',
                            'error': str(exc)})
                        current = []
                        failure = {'op': None, 'phase': 'commit', 'error': exc}

        if failure is None:
            return self._receipt(
                revision, digest, run_id, attempt,
                state='complete',
                completed_operations=list(range(len(operations))),
                committed_sql_segments=committed_sql_segments,
                external_operations=external_operations)

        # Roll back the still-open SQL segment.  Earlier committed segments and
        # completed VACUUMs stay real and are reported below.
        if current or self.connection.in_transaction:
            if self.connection.in_transaction:
                try:
                    self.connection.execute('ROLLBACK')
                    if current:
                        rolled_back_ops.extend(current)
                        rolled_back_sql_segments.append({'operations': list(current)})
                except Exception as rollback_exc:
                    uncertain_segments.append({
                        'operations': list(current), 'phase': 'rollback',
                        'error': str(rollback_exc)})
            elif current:
                rolled_back_ops.extend(current)
                rolled_back_sql_segments.append({'operations': list(current)})
            current = []

        partial = bool(committed_sql_segments or external_operations or uncertain_segments)
        error = f"{failure['phase']}: {failure['error']}"
        return self._receipt(
            revision, digest, run_id, attempt,
            state='partial' if partial else 'failed',
            completed_operations=sorted(set(completed)),
            committed_sql_segments=committed_sql_segments,
            external_operations=external_operations,
            rolled_back_operations=rolled_back_ops,
            rolled_back_sql_segments=rolled_back_sql_segments,
            uncertain_segments=uncertain_segments,
            failed_operation=failure['op'],
            failed_phase=failure['phase'],
            error=error)

    # -- public execution --------------------------------------------------
    def execute(self, revision, operations, *, retry=False):
        """Execute one revision's operations and return an accurate receipt.

        ``retry=True`` is the explicit opt-in that permits a new attempt after a
        previous *clean* failure (no commit and a successful rollback).  It never
        permits replay after a ``partial`` attempt and never permits changing the
        operation definition bound to ``revision``.
        """
        if not isinstance(revision, str):
            raise InvalidOperation('revision id must be a string')
        definition = _normalize_operations(operations)
        definition_text = _definition_text(definition)
        digest = _definition_digest(definition_text)

        blocked = self._preflight(revision, digest, retry)
        if blocked is not None:
            return blocked

        run_id, attempt = self._start_run(revision, digest, definition_text)
        receipt = self._execute_operations(
            revision, definition, digest, run_id, attempt)
        self._finish_run(run_id, receipt)
        return receipt

    # -- graph integration -------------------------------------------------
    @staticmethod
    def _ancestors(parents, revision):
        seen = set()
        stack = [revision]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(parents[current])
        return seen

    def applied_heads(self, graph):
        """Maximal, ancestor-closed complete revisions according to the database.

        A complete revision is only treated as successful if its whole graph
        ancestor closure is also complete.  This keeps a direct out-of-order
        ``execute`` from pretending a missing parent succeeded.
        """
        parents = graph.parents
        complete = set(self.complete_revisions()) & set(parents)
        closed = {revision for revision in complete
                  if self._ancestors(parents, revision) <= complete}
        heads = []
        for revision in closed:
            covered = any(
                other != revision and revision in self._ancestors(parents, other)
                for other in closed)
            if not covered:
                heads.append(revision)
        return sorted(heads)

    def load_heads(self, graph, *, stored=False):
        """Load the success frontier into ``graph`` and return it.

        By default the frontier is recomputed from durable complete run records
        and the graph's ancestors.  ``stored=True`` loads the frontier last
        persisted by ``upgrade`` instead.
        """
        heads = self.stored_heads() if stored else self.applied_heads(graph)
        graph.set_applied_heads(heads)
        return heads

    def _persist_heads(self, graph):
        heads = sorted(graph.applied_heads)
        self._begin_metadata()
        try:
            self.connection.execute(f'DELETE FROM {METADATA_PREFIX}heads')
            for head in heads:
                self.connection.execute(
                    f'INSERT INTO {METADATA_PREFIX}heads(revision_id,recorded_at) '
                    'VALUES(?,?)', (head, _now()))
            self._commit_metadata()
        except Exception:
            self._rollback_metadata()
            raise

    def _verify_backing(self, graph):
        """Reject a graph whose claimed success is not backed by durable records."""
        complete = set(self.complete_revisions())
        unbacked = sorted(set(graph.successful()) - complete)
        if unbacked:
            raise ExecutorStateError(
                'graph success frontier claims revisions with no complete run '
                f'record in this database: {unbacked}; reload heads with '
                'load_heads() instead of inventing them')

    def upgrade(self, graph, revisions, targets, *, retry=False):
        """Execute a graph plan, stopping at the first non-complete receipt.

        Only a fully successful revision updates ``graph`` and the persisted
        applied heads.  A failure or partial receipt stops the run immediately;
        later revisions are neither executed nor stamped.

        ``graph`` must already reflect durable success, typically via
        ``load_heads`` (or by being built with applied heads that really are
        complete in this database).  Revisions already complete in the database
        are treated as successful even when ``revisions`` omits their definition;
        a fresh graph therefore never asks to replay them.
        """
        complete = set(self.complete_revisions())
        # Reconcile the graph with durable success without inventing anything:
        # every DB-complete revision whose graph ancestors are all DB-complete
        # is genuinely successful for this graph.
        for revision in graph.topological_order():
            if revision in complete and revision not in graph.successful():
                if set(self._ancestors(graph.parents, revision)) <= complete:
                    graph.record_success(revision)
        self._verify_backing(graph)
        plan = graph.plan(targets)
        missing = [revision for revision in plan if revision not in revisions]
        if missing:
            raise ExecutorStateError(
                'upgrade plan needs operation definitions for revisions: '
                f'{missing}')
        receipts = []
        for revision in plan:
            receipt = self.execute(revision, revisions[revision], retry=retry)
            receipts.append(receipt)
            if receipt.get('state') != 'complete':
                break
            graph.record_success(revision)
            self._persist_heads(graph)
        else:
            # A no-op upgrade still leaves durable heads consistent with the
            # graph, but never drops a stored head this (perhaps partial) graph
            # does not contain.
            stored = set(self.stored_heads())
            if stored <= set(graph.parents) and stored <= graph.successful():
                if sorted(graph.applied_heads) != sorted(stored):
                    self._persist_heads(graph)
        return receipts
