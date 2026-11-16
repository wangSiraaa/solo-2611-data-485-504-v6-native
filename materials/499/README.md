# Warehouse delegation and routine runtime

Python 3.11 and its standard SQLite module are sufficient. `grant_catalog.py`
is the production grant-directory API; direct catalog callers do not use the
routine runtime. `routine_runtime.py` runs registered read-only data routines and
also exposes `query` for a direct session query. `examples.json` contains one
small synthetic warehouse, a diamond grant graph and nested routines.

The authoritative task is `prompts/499.txt` in the question bundle. The starting
code runs ordinary direct grants, ordinary invoker queries and a definer query.
It is an incomplete implementation of that contract, not an oracle. Extend the
existing revoke API with `grant_option_only=False`; keep its ordinary call form.
Registered actions and relation names are trusted configuration, not arbitrary
SQL. All routine calls are callable; only SELECT is governed by this catalog.

The author-side scripts outside this materials directory observe the starting
point. Their manually stated paths and expected identities are test fixtures,
not production implementations or a complete answer.
