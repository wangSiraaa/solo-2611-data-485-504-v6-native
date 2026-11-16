# Revision graph and real migration execution

`revision_graph.py` is a public offline catalog API. `executor.py` also accepts
operation segments directly without a revision graph. Both have ordinary
runnable paths. `examples.json` holds a diamond and a real SQL/VACUUM failure.
Python 3.11 and SQLite suffice; mature migration libraries are allowed.

The current executor really commits a SQL prefix before VACUUM, then incorrectly
reports a later failure as a complete rollback. The starting graph follows one
parent and loses independent heads. These are starting defects, not expected
migration behavior. The task prompt defines durable records, retry boundaries
and graph propagation; it does not require compensating committed DDL.
