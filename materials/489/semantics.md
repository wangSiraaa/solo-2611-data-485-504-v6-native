# CSV table transaction contract

`TableTransactions(path,total_limit=100)` is also used directly by non-SQL tools.
The CSV has unique integer `id` and nonnegative integer `value`. `rows()` returns
the participant's current private rows during a transaction and committed file
rows otherwise. `begin`, `put`, `remove`, `savepoint(number)`,
`rollback_to(number)`, `release(number)`, `prepare`, `commit`, `rollback` remain
callable. Direct callers prepare before commit.

Savepoint numbers designate the currently live nested scopes. Rolling back to a
savepoint restores its creation state, drops younger scopes, and leaves the
target scope active. Releasing it removes it and younger scopes without making
the outer transaction committed. The same numeric depth may later be reused.
Private rows and live savepoints must not alias in a way that changes a saved
state. No external writer modifies the CSV during one operation sequence.

`CsvModule` registers a real APSW writable virtual table. SQLite is responsible
for SQL, ordinary-table constraints and callbacks. The virtual table must join
the connection's actual transaction and savepoint outcome. An ordinary SQL
constraint error followed by explicit transaction rollback must leave both the
ordinary table and CSV at their prior committed contents. The application does
not promise that every isolated failed statement automatically aborts an
otherwise explicitly open transaction; follow SQLite's actual transaction state.

At prepare/xSync, the sum of all values must not exceed `total_limit`. Failure
rejects the transaction and cannot publish changed CSV rows. The environment
guarantees that after successful prepare, the final local temporary-file write
and replacement succeed; the task does not claim atomic cross-resource recovery
after process kill, disk failure during xCommit, or power loss. It does require
correct visibility through public participant reads, SQL reads and reopened CSV
contents throughout still-running-process rollback/commit boundaries.

The supported SQL mutates values, inserts and deletes rows, and changes integer
ids. The id is the external row's stable lookup identity and virtual rowid;
an UPDATE of the id must move that row, rather than leaving two identities or
discarding its value. Direct writes to the SQL `rowid` pseudo-column are outside
this task, so only the id field controls identity. INSERT and UPDATE support
ABORT (also the default), IGNORE, and REPLACE for duplicate ids, with the same
row outcomes and statement/outer-transaction boundaries as a native SQLite table
`id INTEGER PRIMARY KEY, value INTEGER NOT NULL CHECK(value>=0)`. Inputs have
nonnegative integer values; only duplicate-id constraints are under test here.
ABORT undoes the current statement and preserves earlier statements in an open
outer transaction; IGNORE skips only conflicting rows; REPLACE substitutes the
new row. A failed statement cannot publish any still-private rows to the file.
Conflict modes FAIL and ROLLBACK are outside the supported SQL family.

Up to 100 rows and four live savepoints, one connection and one participant are
sufficient. Full private
snapshots, journals, and mature APSW/SQLite facilities are allowed. The existing
public participant contract must work independently of the SQL adapter.

`probe.py` uses temporary files. `SnapshotParticipant` in that probe is an
isolating test adapter for the virtual-table check; it is not a third production
module or the requested final implementation. Run it with Python 3.11 plus
APSW. It reports observed rows as well as separately stated expected rows.
