"""Output-frontier service. Buffering producers may own holds without using timers."""
class WatermarkTracker:
    def __init__(self, source_names, publish):
        self.sources = dict.fromkeys(source_names, -1)
        self.published = -1
        self.publish = publish
        self._owners = {}
        self._held_times = set()

    def hold(self, owner, timestamp):
        if timestamp < self.published:
            raise ValueError('cannot acquire a hold below the published frontier')
        if owner in self._owners:
            self.release(owner)
        self._owners[owner] = timestamp
        self._held_times.add(timestamp)
        self._refresh()

    def release(self, owner):
        timestamp = self._owners.pop(owner, None)
        if timestamp is not None:
            self._held_times.discard(timestamp)
        self._refresh()

    def update_source(self, source, watermark):
        if source not in self.sources or watermark < self.sources[source]:
            raise ValueError('unknown source or source regression')
        self.sources[source] = watermark
        self._refresh()

    def _refresh(self):
        candidate = min([*self.sources.values(), *self._held_times])
        if candidate > self.published:
            self.published = candidate
            self.publish(candidate)

