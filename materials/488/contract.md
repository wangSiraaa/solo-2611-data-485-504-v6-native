The fixed source image has 16 blocks of 16 bytes. All mutations use ChangedBlocks.
image_id is immutable and identifies a source incarnation, not a path. Every
write increments its generation even when the bytes equal the old bytes.
freeze creates an immutable changed-block set since the confirmed generation and
a readable image cut at end_generation. Later writes cannot alter this set or
its block bytes. read_snapshot may read any block of that cut. Multiple snapshots
may be alive. ack confirms only changes at/before that snapshot's end generation;
later writes remain dirty, and an older ack cannot regress confirmed progress.
Acknowledged snapshots may be retired and reject further reads. The direct API
also serves mirror/checkpoint tools which never create BackupChain products.

A base contains every logical block. A delta names one exact parent backup and
the same image identity; its base_generation equals the parent's end_generation.
Its changed blocks cover every mutation since that base, with bytes from its
frozen end cut. Chain identities, schema/block sizes, all block payloads and the
complete target content must be verified before a restore is reported successful.
Keep enough explicit parent/content identity to reject a different or substituted
ancestor. Imported valid manifests/payloads use the same public restore entry;
they need not have been produced by this tracker. Missing/cyclic ancestors,
invalid payload or identity mismatch are failures, never partial successes.
Restore uses only backup products and can run after the original source is gone.
No hole, compression, deduplication, file resizing, or source rollback is required.

Creating a backup must finish and publish its valid product before acknowledging
the corresponding snapshot. A failed backup cannot erase outstanding tracking
changes or replace an existing successful backup. No process-kill durability or
concurrent threads are required; the harness interleaves public operations.
The implementation may use full copies, generation logs, COW, SQLite, mature
snapshot facilities, shared implementation, or serial execution.

All formal source payload reads, including freeze/COW preservation, prefetch,
copy, validation and retries, must go through BlockIO.read and be counted.
Write arguments already contain their actual new bytes and need not be reread.
Backup-product reads are a separate, truthfully reported cost; they are not source
payload reads. Independent test oracles may read source files directly and do not
serve the production path. Do not hide a complete preloaded source outside this
counter. In the fixed cold incremental benchmark, create a base, reset the source
read ledger, change exactly blocks 3 and 5 once, then create their delta with no
further mutations. That whole delta operation returns at most 4 source blocks.
General legal inputs require correctness and honest cost, not a universal saving.
Scope: 3 retained backup points, at most 40 writes/public operations, one process.
