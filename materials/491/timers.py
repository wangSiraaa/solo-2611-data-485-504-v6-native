"""A synchronous keyed event-time timer service, also used without stream watermarks."""
import heapq
import itertools


class TimerService:
    def __init__(self):
        self.watermark = -1
        self._sequence = itertools.count()
        self._pending = []
        self._active = {}

    def set(self, identity, deadline, output_timestamp, callback):
        if deadline <= self.watermark:
            raise ValueError('deadline must be strictly in the future')
        if output_timestamp > deadline:
            raise ValueError('output timestamp exceeds deadline')
        entry = (deadline, next(self._sequence), identity, output_timestamp, callback)
        self._active[identity] = entry
        heapq.heappush(self._pending, entry)

    def delete(self, identity):
        return self._active.pop(identity, None) is not None

    def advance(self, watermark):
        if watermark < self.watermark:
            raise ValueError('input watermark regressed')
        self.watermark = watermark
        while self._pending and self._pending[0][0] <= watermark:
            deadline, order, identity, output_timestamp, callback = heapq.heappop(self._pending)
            self._active.pop(identity, None)
            callback(identity, deadline, output_timestamp)

