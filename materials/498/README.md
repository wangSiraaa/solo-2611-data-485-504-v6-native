# Local descriptor handoff

The public stream channel serves both file-tool clients and shard workers.
`rights_channel.py` and `shard_host.py` are production starting points;
`examples.json` is a tiny fixture. Python 3.11 and POSIX SCM_RIGHTS suffice.

The wire interface is a 4-byte network-order JSON length followed by UTF-8 JSON.
Ancillary descriptors accompany the first successfully sent byte of that frame.
The target behavior and lifecycle boundaries are defined by the question prompt.
The starting point works for an ordinary single bundle and a three-line worker;
it is incomplete for stream framing and unsuccessful worker lifecycles.

Author-side probes outside this material folder are observations of the starting
point, not a complete channel, host implementation or answer.
