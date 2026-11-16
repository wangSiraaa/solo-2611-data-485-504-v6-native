# Transactional warehouse outbox and relay claims

Python 3.11 and its SQLite module suffice. `warehouse.py` owns fact changes,
stable outgoing envelopes and operation receipts. `relay.py` owns claims and
delivery attempts for existing envelopes; it does not mutate facts. The schema
helper is shared storage support, not a third production responsibility.
`examples.json` contains synthetic rows and an independently seeded event.

The complete contract is the authoritative `prompts/502.txt` in this bundle.
The starter runs ordinary commits and confirmations and already releases the
write transaction before sending. Keep that normal progress. The starter is
incomplete at the fact/outbox failure boundary and stale claim completion.

The probe outside materials uses actual temporary SQLite databases and a
thread/Event transport gate. Its expected outcomes are independent examples,
not a supplied correct relay or warehouse implementation. No real external
message broker, secret, credentials, or notification system is required.
