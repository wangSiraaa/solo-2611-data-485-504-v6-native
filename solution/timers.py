"""Synchronous keyed event-time timer service.

Public surface (also used by callers that do not use stream watermarks):

    TimerService()
    .watermark                     current timer watermark, initially -1
    .set(identity, deadline, output_timestamp, callback)
    .delete(identity) -> bool
    .advance(watermark)

Timer identity is the tuple ``(key, namespace, id)``.  ``set`` replaces any
previous registration with the same identity; ``delete`` suppresses it.  A
replacement is a fresh registration (new registration order).  Old heap
entries that have been replaced, deleted or already fired are tombstones:
they are skipped when they surface and can never affect a later registration
with the same identity.

``advance(w)`` fires every *live* timer whose deadline is <= w, ordered by
``(deadline, registration order)``.  Callbacks may delete other timers that
are already due, and may set/re-set timers; a newly set deadline must be
strictly greater than the service's current watermark.
"""

import heapq
import itertools


class TimerService:
    def __init__(self):
        self.watermark = -1
        self._sequence = itertools.count()
        # Heap entries are tuples: (deadline, seq, identity, output_timestamp, callback)
        self._pending = []
        # identity -> the one live entry for that identity (tombstone detection)
        self._active = {}

    def set(self, identity, deadline, output_timestamp, callback):
        if deadline <= self.watermark:
            raise ValueError('deadline must be strictly greater than the current watermark')
        if output_timestamp > deadline:
            raise ValueError('output timestamp cannot exceed deadline')
        # Replacing an identity: its old heap entry becomes a tombstone because
        # _active now points at the new entry. This is a fresh registration,
        # hence a fresh sequence number (new registration order).
        entry = (deadline, next(self._sequence), identity, output_timestamp, callback)
        self._active[identity] = entry
        heapq.heappush(self._pending, entry)

    def delete(self, identity):
        # Drop the live marker; the (possibly still-heaped) entry becomes a
        # tombstone and will never fire.
        return self._active.pop(identity, None) is not None

    def advance(self, watermark):
        if watermark < self.watermark:
            raise ValueError('timer watermark cannot regress')
        self.watermark = watermark
        while self._pending:
            deadline, seq, identity, output_timestamp, callback = self._pending[0]
            if deadline > watermark:
                break
            heapq.heappop(self._pending)
            # Tombstone: deleted, replaced (identity now maps elsewhere), or
            # already retired. Never fires, never disturbs a later registration.
            live = self._active.get(identity)
            if live is None or live[1] != seq:
                continue
            # Retire first, then fire: re-entrant set() for the same identity
            # during the callback installs a genuinely new live registration.
            self._active.pop(identity)
            callback(identity, deadline, output_timestamp)
