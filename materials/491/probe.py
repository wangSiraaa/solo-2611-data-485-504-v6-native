"""Read-only starter observations. Expected values are literal contract examples."""
import json
from timers import TimerService
from watermarks import WatermarkTracker
from stream_operator import BatchOperator, StrictSink


def run():
    normal = []
    timer = TimerService()
    timer.set(('k', 'billing', 't'), 5, 5, lambda *args: normal.append(args[1]))
    timer.advance(5)
    replacement = []
    timer = TimerService()
    key = ('k', 'billing', 't')
    timer.set(key, 5, 5, lambda *args: replacement.append('old'))
    timer.set(key, 8, 7, lambda *args: replacement.append('replacement'))
    timer.advance(10)
    deletion = []
    timer = TimerService()
    timer.set(key, 5, 5, lambda *args: deletion.append('deleted'))
    timer.delete(key)
    timer.advance(10)

    normal_progress = []
    frontier = WatermarkTracker(['a', 'b'], normal_progress.append)
    frontier.hold('external-buffer', 10)
    frontier.update_source('a', 50)
    frontier.update_source('b', 50)
    frontier.release('external-buffer')
    progress = []
    frontier = WatermarkTracker(['source'], progress.append)
    frontier.hold('external-a', 10)
    frontier.hold('external-b', 10)
    frontier.update_source('source', 100)
    frontier.release('external-a')
    before_last_release = frontier.published

    normal_sink = StrictSink()
    operator = BatchOperator(['source'], normal_sink)
    operator.schedule(key, 10, 10, 'ordinary')
    operator.advance('source', 10)
    sink = StrictSink()
    operator = BatchOperator(['source'], sink)
    operator.schedule(key, 90, 20, 'late-unless-held')
    operator.advance('source', 100)

    return {
        'timer_normal': {'actual': normal, 'expected': [5]},
        'timer_replacement': {'actual': replacement, 'expected': ['replacement']},
        'timer_deletion': {'actual': deletion, 'expected': []},
        'frontier_external_normal': {'actual': normal_progress, 'expected': [10, 50]},
        'two_external_owners_same_time': {'actual': before_last_release, 'expected': 10,
            'reason': 'external-b still owns output timestamp 10'},
        'operator_normal': {'accepted': normal_sink.accepted, 'late': normal_sink.late,
            'expected_accepted': [[10, 'ordinary']], 'expected_late': []},
        'output_before_hold_release': {'events': sink.events, 'accepted': sink.accepted,
            'late': sink.late, 'expected_accepted': [[20, 'late-unless-held']],
            'expected_late': [], 'reason': 'watermark 100 forbids a later record at 20'},
    }


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
