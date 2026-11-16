The service is synchronous and deterministic. Timer identity is the tuple
(key, namespace, id). set replaces exactly that identity; delete suppresses it.
advance(w) invokes valid due timers in (deadline, registration order) order.
A callback may delete another due timer, or set a timer whose deadline is strictly
greater than the current timer watermark. Replacement is a fresh registration.
No callback exceptions, threads, clocks, checkpointing, or infinite callbacks.

A public output watermark w promises that no subsequent record has timestamp < w;
records at w are valid. Output timestamp is no greater than the timer deadline.
All source watermarks and public watermarks are monotone. A new hold below the
already public frontier is invalid and must leave existing state unchanged.
Each hold has an owner; different owners can hold the same timestamp. Replacing
one owner's hold cannot expose a transient frontier past the replacement.
The exact eligible frontier after a completed public operation is the minimum of
all source watermarks and all live holds. Initially source/public watermarks=-1.
Sources are nonempty and their names are fixed at construction.

BatchOperator connects these existing public services. A pending timer owns a hold
at its declared output timestamp. During firing, output delivery and callback
changes must be settled before its retired hold can permit a later frontier.
The callback may schedule future output only at or above the current public frontier.
After a public advance returns, due callbacks have completed and the eligible
frontier has been published; buffering all progress until EOF is not allowed.
Other producers may independently use WatermarkTracker and need not use timers.

Limits: 20 keys, at most 4 live timers per key, at most 100 outer operations and
at most 100 callback-created timers; integer times in [0, 100000]. Full scans,
sorting, mature scheduling libraries, shared implementation, and serial execution
are all allowed. Preserve public observable contracts, not a particular layout.
