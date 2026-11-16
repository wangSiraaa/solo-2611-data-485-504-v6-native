Each member is a fixed-length ordinary file, four stripes of 16-byte blocks.
For stripe s, parity is member s%3, with the remaining members in ascending order
holding logical blocks 2s and 2s+1. Parity is bytewise XOR of the two data blocks.
At most one member can be absent. Healthy writes and healthy/degraded reads and
rebuild must preserve that logical view. Writes are issued only with all three
members present. A missing data block can be reconstructed from
the other data and parity; a missing parity block does not block healthy data.
Rebuild writes an absent member to a fresh file without changing good members.

BlockJournal is separately public for arbitrary named ordinary files in its root.
Its apply(request_id,patches) commits one non-overlapping group of byte patches;
the filenames are an existing safe local file set, offsets/lengths are in range.
On success all patches are durable. Interruption after any actual durable step
and reopening/recovery must expose all old or all new group bytes, never a mix.
The implementation must identify its durable commit decision: recovery of a
durably committed group yields all-new bytes; a durable abort yields all-old.
Without either decision, either complete outcome is acceptable. Tests do not
assume that a particular data/parity write is before or after the commit point.
Successful earlier groups remain committed. Retrying an identical request id
does not create a different effect or let old recovery overwrite a later group;
reusing an id for different patches is rejected. Recovery is itself repeatable.
Patch records may cover different files; no parity assumptions belong to this API.
The chosen durable commit representation is part of the implementation, not fixed.

One writer, at most ten requests/ten patches per request, no concurrent readers
during apply/recover, no filesystem rename, resizing, media bitrot, or two-disk
failure. After interruption callers recover before normal reads. Real durable
steps must expose fault hooks for process termination or an injected stop, and
the harness must reopen files rather than fixing them in the exception handler.
SQLite/logging libraries, complete-stripe writes, serial execution, and mature
XOR libraries are allowed. No fixed performance threshold or prohibition on
full stripe reads. Do not count fixture/oracle helpers as production modules.
