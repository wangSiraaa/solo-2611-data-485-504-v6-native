# Data-quality products and operator incident control

Python 3.11 suffices. `quality_runs.py` consumes synthetic batch rows and produces
queryable per-run data-quality evidence. It has users that only read its reports.
`incident_service.py` consumes generic fault observations and operator actions;
external storage incidents use it without the quality service. `examples.json`
contains small good and bad warehouse batches.

The authoritative question in `prompts/504.txt` fixes the status precedence,
sequence gaps, duplicate and late-run behavior, episode identity and logical
silence expiry. The starter handles ordinary valid/NULL batches and ordinary
open/ack/recovery, but is incomplete. It keeps state in process; persistence or
real notification delivery is not required. A local notification list is a
deliverable record, not permission to contact real people.

Author probes outside this directory state independent expected transitions.
They are not a supplied complete state machine or an implementation to import
as the production answer.
