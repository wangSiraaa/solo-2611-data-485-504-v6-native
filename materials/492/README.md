# Revision graph and real migration execution

`revision_graph.py` is a public offline catalog API. `executor.py` is the real
SQLite executor and the graph-aware `upgrade` entry. `examples.json` holds a
normal linear graph, a diamond (`root → a,b → merge`) and the `INSERT → VACUUM →
invalid SQL` partial material. Python 3.11 and its `sqlite3` module suffice; no
third-party migration library is required.

## Calling

Run the end-to-end reproduction (temporary databases, independent SQL checks):

```sh
python3 demo.py
```

Library use:

```python
import sqlite3
from revision_graph import RevisionGraph
from executor import MigrationExecutor

connection = sqlite3.connect("app.db")
executor = MigrationExecutor(connection)          # creates _migration_* tables

# Offline catalog inspection (no database needed):
graph = RevisionGraph({"root": [], "a": ["root"], "b": ["root"],
                       "merge": ["a", "b"]}, applied_heads=())
print(graph.heads(), graph.plan(["merge"]))

# Direct operation segment without a graph:
receipt = executor.execute("rev-1", [
    {"kind": "sql", "sql": "CREATE TABLE t(x)"},
    {"kind": "vacuum"},
    {"kind": "sql", "sql": "INSERT INTO t VALUES(1)"},
])

# Graph-aware upgrade (bridges plan + real execution + durable success):
revisions = {"root": [...], "a": [...], "b": [...], "merge": [...]}
receipts = executor.upgrade(graph, revisions, targets=["merge"])
```

`RevisionGraph(parents, applied_heads)` and `MigrationExecutor(connection)` keep
their original entry points. A revision is a string id mapped to an ordered
parent list of at most two ids. `heads()` returns unreferenced revisions.
`plan(targets)` returns the targets' ancestor closure minus already-successful
revisions, each once, parents before children. `record_success(revision)` only
advances once all parents are successful, drops covered old heads and keeps
independent ones.

`execute` only runs the operation record it is given — it never invents graph
parents. `upgrade` is the only place that ties a graph plan to execution and
durable success/heads; it stops at the first non-`complete` receipt.

## Stored state

Metadata lives in the same SQLite database, under a `_migration_` prefix, so it
is separate from business tables and survives close/reopen:

| table | purpose |
| --- | --- |
| `_migration_definitions` | first-bound operation definition per `revision_id` (canonical JSON + SHA-256 digest). |
| `_migration_runs` | one row per attempt: `state` (`running`/`complete`/`failed`/`partial`), completed operations, committed SQL segments, completed external (VACUUM) operations, rolled-back operations, uncertain segments, failure location. |
| `_migration_complete` | revisions fully committed at least once (the success fact). |
| `_migration_heads` | applied heads last persisted by a graph-aware `upgrade` (for reload). |

Per-revision state is derived from these rows, so after a reopen you can still
distinguish:

* **complete** — a `_migration_complete` row / `complete` run;
* **failed** — no committed side effects, rollback succeeded;
* **partial** — some SQL segment or external operation committed, then a later
  operation failed.

The first `execute` of a `revision_id` binds its normalized operation definition.
Reusing the id with any different definition raises `RevisionDefinitionConflict`
before any SQL runs; changing a revision requires a new id.

## Failure and retry boundaries

* Consecutive `sql` operations form one transaction. Before a `vacuum` the open
  SQL segment is committed and `VACUUM` runs outside any transaction; later SQL
  opens a new transaction.
* On failure only the still-open SQL segment is rolled back. The receipt reports
  the **real** committed SQL segments, completed VACUUMs, rolled-back operations,
  the failing operation and phase, and any segment whose commit result was lost.
  A committed prefix is never relabelled as a full-version rollback, and planned
  operations are never reported as executed.
* After a `partial`, further `execute`/`upgrade` for that revision returns a
  `blocked` receipt (reason `partial_or_incomplete_attempt`) with existing
  evidence. There is no automatic replay, automatic compensation, or automatic
  continuation.
* After a **clean** failure (no commit, successful rollback) a new attempt
  requires an explicit `retry=True` with the same definition. The original
  failure record is preserved; the definition still cannot change.
* Replaying a fully successful revision with the same definition is a no-op.
* No promise is made to roll back already-committed DDL/VACUUM or to recover
  from a process kill at an arbitrary instruction point.

`upgrade` stops on the first `failed`/`partial`/`blocked` receipt, executes no
successor, and does not stamp the failed revision. Durable heads are only
advanced for fully successful revisions, so a reloaded graph matches the real
run records. `load_heads(graph, stored=True)` loads persisted heads;
`load_heads(graph)` recomputes the frontier from complete run records and the
graph's ancestors. `upgrade` refuses a graph whose claimed success is not backed
by durable complete records, and reconciles already-complete revisions so a
fresh graph never replays them.

## Record design choice

Operation segments are recorded per revision and per attempt, with explicit
`committed_sql_segments` (operation index groups) and `external_operations`
indices. This is what explains intermediate commits: a receipt and the durable
run row show exactly which group of SQL had committed before VACUUM and which
external operation ran, independent of the later failure. It also makes the
post-failure plan (nothing automatically replays a partial revision) and the
manual-repair boundary (fix the environment, then either a new revision id or an
explicit `retry=True` after a clean failure) explicit in data rather than prose.

## Reproduction record

`python3 demo.py` prints and independently asserts:

1. **Graph validation** — unknown/duplicate parents, cycles and >2 parents are
   rejected; rejected mutations leave the existing graph unchanged; invalid
   applied heads (duplicate or ancestor+descendant) are rejected.
2. **Planning** — full diamond plan is parents-first and duplicate-free; merge
   needs both parents; independent heads are preserved then collapse at merge.
3. **Linear graph** — `root → l1 → l2 → l3` executes in order, one row each.
4. **Diamond upgrade** — independent branches commit real rows; merge runs once;
   every ancestor's row count is 1 (checked by SQL, plus hand-written ancestor
   sets); after reopen, stored heads reload and a replayed upgrade does nothing.
5. **Partial material** — direct `execute` yields `partial` with committed
   segment `[0]`, external operation `[1]`, failure at operation 2, and the
   pre-VACUUM `INSERT` is still visible by SQL. Through `upgrade`, the failed
   revision is not a head, not `complete`, and stored heads stay at `root`.
   After reopen, `upgrade`/`execute` block replay (no repeated `INSERT`), and a
   changed definition for the same id is rejected before any SQL.
6. **Clean failure** — no committed side effects; default replay is blocked;
   explicit `retry=True` after an external fix succeeds on attempt 2 while the
   original failure evidence is kept.

All checks use real SQLite schema/row queries and hand-written ancestor sets,
not only the tool's own returned version strings.
