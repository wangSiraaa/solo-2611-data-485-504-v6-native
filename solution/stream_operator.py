"""Batch operator wiring timers to an output watermark and a strict sink.

Ownership scheme
----------------
Every timer *registration* (not every timer identity) owns its own hold on
the tracker, keyed by a unique token ``(identity, serial)``.

* ``_tokens``     - identity -> (token, deadline) of its currently *live*
                    (not replaced) registration
* ``_tombstones`` - heap of ``(deadline, token)`` entries for registrations
                    replaced before firing. A replacement cannot free the old
                    hold immediately: the old heap entry has not yet been
                    scanned, and another key's hold might be pinning the
                    frontier at exactly this timestamp. The hold is retired
                    only once an advance reaches the old entry's deadline,
                    i.e. when the timer service has provably skipped it.

A deleted registration, by contrast, retires its hold immediately: once
deleted it can never produce a record, so there is nothing left to protect.

Replacing or deleting a timer therefore never removes the protection of a
later registration with the same identity, and a callback that re-registers
its own identity cannot have its fresh hold deleted by the old registration's
cleanup.

Firing ordering
---------------
Within one timer callback the order is:

  1. the result record is handed to the sink (the hold is still live, so the
     public watermark cannot pass the record's timestamp first);
  2. the callback's re-entrant scheduling/deletions run and settle;
  3. only then is this registration's hold released, which may publish the
     next eligible frontier.

So a timer declared with deadline 90 / output timestamp 20, with the input
watermark advancing to 100, delivers its record at 20 before any watermark
above 20 reaches the sink.
"""

import heapq
import itertools

from timers import TimerService
from watermarks import WatermarkTracker


class StrictSink:
    """A record whose timestamp is below the current watermark is late."""

    def __init__(self):
        self.watermark = -1
        self.events = []
        self.accepted = []
        self.late = []

    def progress(self, watermark):
        self.watermark = watermark
        self.events.append(['watermark', watermark])

    def record(self, timestamp, value):
        self.events.append(['record', timestamp, value])
        bucket = self.late if timestamp < self.watermark else self.accepted
        bucket.append([timestamp, value])


class BatchOperator:
    def __init__(self, sources, sink):
        self.sink = sink
        self.timers = TimerService()
        self.frontier = WatermarkTracker(sources, sink.progress)
        self._serials = itertools.count()
        # identity -> (token, deadline) of the currently live registration
        self._tokens = {}
        # (deadline, token) for replaced registrations whose hold must stay
        # protected until an advance has scanned past that heap entry
        self._tombstones = []

    def schedule(self, identity, deadline, output_timestamp, value, after_emit=None):
        # Acquire the protection first. If the timestamp were below the
        # published frontier this raises without mutating any state; outer
        # schedules are contractually in range, but keep the rollback anyway.
        token = (identity, next(self._serials))
        self.frontier.hold(token, output_timestamp)
        try:
            self.timers.set(identity, deadline, output_timestamp,
                            self._make_callback(token, identity, value, after_emit))
        except BaseException:
            self.frontier.release(token)
            raise
        previous = self._tokens.get(identity)
        self._tokens[identity] = (token, deadline)
        if previous is not None:
            # Old registration is now a tombstone inside the timer service.
            # If its deadline is already at or below the timer watermark the
            # heap entry has effectively been scanned (or will be skipped in
            # the current firing round), so retire the hold straight away;
            # otherwise keep it protected until an advance reaches it.
            old_token, old_deadline = previous
            if old_deadline <= self.timers.watermark:
                self.frontier.release(old_token)
            else:
                heapq.heappush(self._tombstones, (old_deadline, old_token))

    def delete(self, identity):
        # A deleted registration can never fire again, so retiring its hold
        # right away is safe. Suppress the timer first, then release.
        removed = self.timers.delete(identity)
        previous = self._tokens.pop(identity, None)
        if previous is not None:
            self.frontier.release(previous[0])
        return removed

    def advance(self, source, watermark):
        # Publishing source progress first is safe: every pending timer still
        # holds its output timestamp, so the frontier cannot overtake an
        # undelivered result. Then fire everything due at the global input
        # watermark (the minimum across sources); firing callbacks release
        # holds and publish further progress as records are delivered.
        self.frontier.update_source(source, watermark)
        self.timers.advance(min(self.frontier.sources.values()))
        self._drain_tombstones()

    def _drain_tombstones(self):
        # The timer service processes heap entries ordered by deadline; once
        # its watermark reaches a tombstone's deadline, that entry has been
        # scanned and skipped, so its hold may finally be retired.
        wm = self.timers.watermark
        while self._tombstones and self._tombstones[0][0] <= wm:
            _, token = heapq.heappop(self._tombstones)
            self.frontier.release(token)

    def _make_callback(self, token, identity, value, after_emit):
        def on_fire(fired_identity, deadline, output_timestamp):
            # The timer service only invokes live registrations, so this token
            # is still a current hold owner at this point.
            # 1. Deliver the real record while the hold protects it.
            self.sink.record(output_timestamp, value)
            # 2. Let the callback create/replace/delete timers; any new
            #    registration acquires its own hold before this one is released.
            if after_emit is not None:
                after_emit(self)
            # 3. Retire this registration only. If the callback re-registered
            #    the same identity, _tokens points at the new token and this
            #    release cannot remove the new registration's protection.
            if self._tokens.get(identity, (None,))[0] is token:
                self._tokens.pop(identity, None)
            self.frontier.release(token)

        return on_fire
