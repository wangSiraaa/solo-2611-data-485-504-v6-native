# Transaction evidence and callback ownership

Python 3.11 and sqlite3 suffice. `backend.py` is a support fixture with real
SQLite transactions and explicit injected driver-protocol labels. SQLite is
not claimed to emit the fixture SQLSTATE codes natively. Do not count this
fixture as a missing production module or an answer.

`session.py` is the public direct SQL client. `transaction_runner.py` is the
callback-level transaction owner. `examples.json` is small schema/input material.
The authoritative prompt fixes error evidence, retry ownership and unknown
COMMIT outcomes. Mature retry helpers and new connections per attempt are legal.
The starter runs ordinary commits but loses the original failure and retries an
already committed callback when its confirmation is lost.
