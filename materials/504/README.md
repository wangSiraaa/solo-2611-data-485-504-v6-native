# Data-quality products and operator incident control

Python 3.11 suffices; no third-party dependency. Three production-facing pieces:

* `quality_runs.py` — `QualityRuns` turns synthetic batch rows into queryable
  per-run data-quality evidence. Its users only read reports.
* `incident_service.py` — `IncidentService` consumes generic fault observations
  and operator actions (ack, silence, unsilence, logical clock). External
  storage incidents use it without the quality service.
* `pipeline.py` — `QualityIncidentBridge` is the one real integration point. It
  ingests a batch and forwards that run's derived quality observation to the
  incident service under the same `check`/`run_id`/`sequence` identity.

`examples.json` supplies a small good and a bad warehouse batch.
Run `python3 demo.py` for step-by-step verification against independent expected
state tables; persistence and real notification delivery are not required.

## Quality evidence (`QualityRuns.ingest` / `.report`)

Identity is `(check, run_id)`. Canonical content is `check, run_id, sequence,
rows, expected_ids, complete`: JSON object key order is ignored, but the order
of `rows` and `expected_ids` arrays is preserved (reordering is a conflict).

Status precedence per run:

1. any observed row with `value is None` → `FAIL`
2. else `complete=False` → `UNKNOWN`
3. else missing expected ids → `FAIL`
4. else complete coverage and no NULL → `PASS`

An empty `expected_ids` with `complete=True` is a legitimate, fully-covered empty
batch (`PASS`); the same empty input with `complete=False` is `UNKNOWN`.

Re-sending an identical identity+content returns the original result and moves
no counter. Different content under an existing identity is rejected with
`QualityInputError` and the original record is kept. A `sequence` may not be
reused by a different `run_id` within a check.

Only a run whose `sequence` exceeds the check frontier updates the current
result and consecutive evidence. A late, smaller-sequence run keeps its own row
evidence in history but rewrites neither the current result nor any fault
decision. Consecutive evidence counts adjacent sequences of distinct runs; a
sequence gap or an `UNKNOWN` breaks a PASS/FAIL streak. Two consecutive FAILs
give `observation=FAULT`, two consecutive PASSes give `HEALTHY`, otherwise
`UNKNOWN`. This derived observation never changes a run's own status. `report`
returns deep copies, so operator actions and caller mutation cannot alter it.

## Fault episodes (`IncidentService`)

An observation is `(key, observation_id, sequence, value)` with `value ∈
{FAULT, HEALTHY, UNKNOWN}`; canonical content is exactly those four fields.
Identity is `(key, observation_id)` with the same duplicate/conflict, sequence
ownership and late-retention rules as quality runs.

For a key with no OPEN episode, `FAULT` opens a new episode identity. A `FAULT`
on the OPEN episode stays in it. `HEALTHY` moves the OPEN episode to
`RECOVERED`; `UNKNOWN` leaves the fault state unchanged. A `FAULT` after
recovery necessarily creates a new identity and clears the old ACK.

* `ack(key)` acknowledges the current OPEN episode, is rejected when nothing is
  OPEN, and does not change whether a fault exists.
* `silence(key, until)` suppresses that key's notifications until expiry, may
  be set before any fault, may replace a deadline, must be `> clock`, and never
  recovers an OPEN episode.
* `advance(tick)` moves a non-decreasing clock and expires silences at
  `tick >= until`; `unsilence` releases early (repeating it is a no-op).

At most one notification is recorded per episode, when it is OPEN,
unacknowledged, unnotified and not silenced. A notice suppressed at creation is
recorded once later when the silence is released/expires and the episode is
still eligible. Repeat observations and clock advances never re-notify; recovery
never backfills; ACK never revokes a recorded notice. This is a local record
only — no real mail or pager is contacted. Unsolicited failures (`ack` with no
OPEN fault, invalid value, regressing clock, `until <= clock`, identity/sequence
conflict) raise `IncidentError` before any partial state change, and every
query returns fresh copies.

## Two organization styles and their tradeoffs

**Replay retained evidence.** Keep the small canonical inputs per identity and
recompute results on query. Late evidence is trivial: replay in sequence order
and the frontier/streak/episode are naturally correct, and duplicate identity is
just a lookup. The cost is repeated computation as history grows, which is fine
inside the stated 20-run/observation bounds but scales poorly.

**Maintain current state plus recorded decisions.** Keep the frontier, current
result, streak counts, episode identities and notification flags, optionally in
SQLite. Queries are cheap. The risk is late or duplicate input silently
rewriting an already-made fault decision, so every mutation must first check the
frontier and identity and record *why* it was accepted or ignored. This
implementation takes that path, keeping immutable per-identity evidence next to
the current state so both the decision and its basis remain queryable.

Either style must preserve the same three invariants: a late run keeps its own
evidence but does not rewrite current/fault decisions; episode identity changes
only on a FAULT that follows non-OPEN state; and notification dedup is keyed to
the specific episode (plus silence eligibility), so operator actions and repeats
never fabricate or duplicate a notice.
