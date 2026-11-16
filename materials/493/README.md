# Function binding and bound invocation

The catalog binds metadata without evaluating actual expressions. The runtime
also accepts externally produced valid plans, so it can be checked independently
of the catalog. `examples.json` contains tiny overload metadata; actual functions
are registered by a caller, never evaluated from arbitrary source text.

Python 3.11 suffices. Ordinary exact integer invocation is runnable. The starter
is incomplete for overload types/ambiguity, named argument source order, explicit
NULL/default separation, exact decimal casts and strict invocation. The prompt
fixes the finite ranking rule; it is not a claim of complete PostgreSQL behavior.
