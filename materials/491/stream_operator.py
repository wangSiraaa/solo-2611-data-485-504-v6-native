"""Small data operator; its real sink rejects records older than its public watermark."""
from timers import TimerService
from watermarks import WatermarkTracker


class StrictSink:
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
        (self.late if timestamp < self.watermark else self.accepted).append([timestamp, value])


class BatchOperator:
    def __init__(self, sources, sink):
        self.sink = sink
        self.timers = TimerService()
        self.frontier = WatermarkTracker(sources, sink.progress)

    def schedule(self, identity, deadline, output_timestamp, value, after_emit=None):
        self.frontier.hold(identity, output_timestamp)

        def on_fire(key, due, timestamp):
            self.frontier.release(key)
            self.sink.record(timestamp, value)
            if after_emit is not None:
                after_emit(self)

        self.timers.set(identity, deadline, output_timestamp, on_fire)

    def delete(self, identity):
        self.timers.delete(identity)
        self.frontier.release(identity)

    def advance(self, source, watermark):
        self.frontier.update_source(source, watermark)
        self.timers.advance(min(self.frontier.sources.values()))

