# Deferred relation integrity and transaction scopes

Use Python 3.11. No third-party dependency is required for the starter.
`constraint_catalog.py` is a relation and action-preflight production service,
also used by a nontransactional offline importer. `transaction_store.py` is the
production bounded transaction store. `examples.json` supplies a small parent
and child relation and its composite foreign-key definition.

The complete contract is the authoritative question `prompts/500.txt` in this
bundle. Foreign keys are MATCH SIMPLE, not MATCH FULL. Only delete uses the
declared RESTRICT action; updates use NO ACTION. Rows and mode changes have the
specified transaction/savepoint lifecycle. The starter supports ordinary valid
relations and a deferred child-then-parent commit but is incomplete.

SQLite is available through the standard library for independent checks of its
supported foreign keys; it does not natively implement this task's named
SET-CONSTRAINTS API. The author probe is outside materials and its isolated
exact checker is an oracle fixture, not a supplied production implementation.
