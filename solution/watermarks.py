"""Output-frontier service for buffering producers.

Independent of the timer service: any buffering producer can own holds here.

    WatermarkTracker(source_names, publish)
    .published                    public output watermark, initially -1
    .sources                      fixed source name -> source watermark (-1)
    .hold(owner, timestamp)       acquire, or atomically replace, an owner's hold
    .release(owner)
    .update_source(source, watermark)

A hold says "this owner may still deliver a record at this timestamp".
Different owners may hold the same timestamp; each owner is tracked
separately, so releasing one owner never releases another owner's hold.

After every completed public operation the eligible frontier is the minimum
of all source watermarks and all live hold timestamps.  Progress is
published as soon as it exists (strictly greater than the previous public
watermark).  A hold below the already published frontier is rejected and
leaves all existing state untouched.
"""


class WatermarkTracker:
    def __init__(self, source_names, publish):
        names = list(source_names)
        if not names:
            raise ValueError('at least one source is required')
        self.sources = dict.fromkeys(names, -1)
        self.published = -1
        self.publish = publish
        # owner -> held timestamp. Owners are distinct keys, so identical
        # timestamps held by several owners never collapse into one entry.
        self._owners = {}

    def hold(self, owner, timestamp):
        if timestamp < self.published:
            # Reject before touching anything: existing state must survive.
            raise ValueError('cannot hold a timestamp below the published frontier')
        # Atomic replacement: swap the owner's timestamp and evaluate the new
        # frontier once, so no transient frontier past the replacement hold
        # can be published during the operation.
        self._owners[owner] = timestamp
        self._refresh()

    def release(self, owner):
        if owner in self._owners:
            del self._owners[owner]
            self._refresh()

    def update_source(self, source, watermark):
        if source not in self.sources:
            raise ValueError('unknown source')
        if watermark < self.sources[source]:
            raise ValueError('source watermark cannot regress')
        if watermark == self.sources[source]:
            return
        self.sources[source] = watermark
        self._refresh()

    def _frontier(self):
        # sources is non-empty by construction, so min always has an argument.
        candidate = min(self.sources.values())
        if self._owners:
            candidate = min(candidate, min(self._owners.values()))
        return candidate

    def _refresh(self):
        candidate = self._frontier()
        if candidate > self.published:
            self.published = candidate
            self.publish(candidate)
