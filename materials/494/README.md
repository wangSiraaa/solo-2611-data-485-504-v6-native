# Reverse host lookups

`duplex_rpc.py` is an existing two-way endpoint API used independently of jobs.
It currently supports an ordinary call but rejects a reverse request while it
is waiting. `host_catalog.py` is a separately callable scope service; ordinary
lookups work, while snapshot/field/peer/terminal checks are incomplete.

`examples.json` contains a synthetic product directory. The public wire is one
UTF-8 JSON-RPC object per line. Python 3.11 socketpair suffices. Mature dispatch
libraries are allowed. The prompt defines scope lookup's serialization point;
it does not require retroactively retracting a lookup already authorized/read.
