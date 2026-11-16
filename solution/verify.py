"""Verification for timers / watermarks / BatchOperator.

Run:  python3 verify.py

Part 1: deterministic scenarios covering every contract requirement.
Part 2: differential fuzz tests against an independent reference model,
        plus end-to-end operator fuzzing with StrictSink invariants.
"""

import random

from timers import TimerService
from watermarks import WatermarkTracker
from stream_operator import BatchOperator, StrictSink


# --------------------------------------------------------------------------
# 1. TimerService
# --------------------------------------------------------------------------

def test_timer_normal():
    out = []
    svc = TimerService()
    svc.set(('k', 'ns', 'a'), 5, 5, lambda i, d, t: out.append(t))
    svc.advance(4)
    assert out == [] and svc.watermark == 4
    svc.advance(5)
    assert out == [5] and svc.watermark == 5
    print('ok  timer normal firing (deadline boundary inclusive)')


def test_timer_ordering():
    order = []
    svc = TimerService()
    svc.set(('k', 'n', 'late-reg-early-dl'), 3, 3, lambda *a: order.append('a'))
    svc.set(('k', 'n', 'early-reg-late-dl'), 5, 5, lambda *a: order.append('b'))
    svc.set(('k', 'n', 'same-dl'), 3, 3, lambda *a: order.append('c'))
    svc.advance(10)
    # (deadline, registration order): a at dl3 first, then c at dl3, then b.
    assert order == ['a', 'c', 'b'], order
    print('ok  timer order is (deadline, registration order)')


def test_timer_replacement_same_identity():
    out = []
    svc = TimerService()
    key = ('k', 'billing', 't')
    svc.set(key, 5, 5, lambda *a: out.append('old'))
    svc.set(key, 8, 7, lambda *a: out.append('replacement'))
    assert len(svc._active) == 1
    svc.advance(10)
    assert out == ['replacement'], out
    # A fresh registration of the same identity afterwards is fully independent.
    svc.set(key, 20, 20, lambda *a: out.append('new-cycle'))
    svc.advance(20)
    assert out == ['replacement', 'new-cycle'], out
    print('ok  same-identity replacement is a fresh registration; old one never fires')


def test_timer_delete_suppresses():
    out = []
    svc = TimerService()
    key = ('k', 'n', 't')
    svc.set(key, 5, 5, lambda *a: out.append('deleted'))
    assert svc.delete(key) is True
    assert svc.delete(key) is False
    svc.advance(10)
    assert out == []
    print('ok  deleted timer never fires; repeated delete is a no-op')


def test_timer_callback_deletes_due_neighbour():
    fired = []
    svc = TimerService()

    def first(*_):
        fired.append('first')
        svc.delete(('k', 'n', 'second'))  # already due, not yet executed

    svc.set(('k', 'n', 'first'), 5, 5, first)
    svc.set(('k', 'n', 'second'), 5, 5, lambda *a: fired.append('second'))
    svc.set(('k', 'n', 'third'), 6, 6, lambda *a: fired.append('third'))
    svc.advance(10)
    assert fired == ['first', 'third'], fired
    print('ok  callback deletes another due-but-unfired timer')


def test_timer_callback_reregister_same_identity():
    fired = []
    svc = TimerService()
    key = ('k', 'n', 'self')
    state = {'round': 0}

    def cb(identity, deadline, output_ts):
        state['round'] += 1
        fired.append((deadline, state['round']))
        if state['round'] == 1:
            # Re-register the SAME identity during its own firing.
            svc.set(identity, 10, 10, cb)

    svc.set(key, 5, 5, cb)
    svc.advance(5)
    assert fired == [(5, 1)]
    svc.advance(10)
    assert fired == [(5, 1), (10, 2)], fired
    print('ok  callback re-registration of the same identity fires later')


def test_timer_callback_sets_future_timer():
    fired = []
    svc = TimerService()

    def parent(identity, deadline, output_ts):
        fired.append('parent')
        svc.set(('k', 'n', 'child'), svc.watermark + 1, svc.watermark + 1,
                lambda *a: fired.append('child'))

    svc.set(('k', 'n', 'parent'), 5, 5, parent)
    svc.advance(5)
    assert fired == ['parent']
    svc.advance(6)
    assert fired == ['parent', 'child']
    print('ok  callback-created timer above current watermark fires later')


def test_timer_replaced_then_deleted_only_kills_new():
    out = []
    svc = TimerService()
    key = ('k', 'n', 'x')
    svc.set(key, 5, 5, lambda *a: out.append('old'))
    svc.set(key, 8, 8, lambda *a: out.append('new'))
    assert svc.delete(key) is True  # deletes the live (new) registration only
    svc.advance(10)
    assert out == [], out
    print('ok  delete after replacement suppresses the live registration only')


def test_timer_deadline_rules():
    svc = TimerService()
    svc.advance(10)
    try:
        svc.set(('k', 'n', 'bad'), 10, 10, lambda *a: None)
    except ValueError:
        pass
    else:
        raise AssertionError('deadline == watermark must be rejected')
    try:
        svc.set(('k', 'n', 'bad2'), 11, 12, lambda *a: None)
    except ValueError:
        pass
    else:
        raise AssertionError('output_timestamp > deadline must be rejected')
    try:
        svc.advance(9)
    except ValueError:
        pass
    else:
        raise AssertionError('watermark regression must be rejected')
    print('ok  deadline strictly above watermark; output_ts <= deadline; no regression')


# --------------------------------------------------------------------------
# 2. WatermarkTracker
# --------------------------------------------------------------------------

def test_tracker_progress_and_holds():
    progress = []
    t = WatermarkTracker(['a', 'b'], progress.append)
    assert t.published == -1 and t.sources == {'a': -1, 'b': -1}
    t.update_source('a', 50)
    assert progress == []  # source b still at -1 is the minimum
    t.hold('buf', 10)
    assert progress == []  # hold at 10 below the source min
    t.update_source('b', 50)
    assert progress == [10]  # hold pins the frontier
    t.release('buf')
    assert progress == [10, 50]
    print('ok  frontier is min(source watermarks, live holds); progress published eagerly')


def test_tracker_two_owners_same_time():
    progress = []
    t = WatermarkTracker(['source'], progress.append)
    t.hold('external-a', 10)
    t.hold('external-b', 10)
    t.update_source('source', 100)
    assert progress == [10]
    t.release('external-a')
    assert t.published == 10 and progress == [10], progress
    t.release('external-b')
    assert t.published == 100 and progress == [10, 100]
    print('ok  two owners holding the same timestamp release independently')


def test_tracker_hold_replacement_no_transient_overshoot():
    progress = []
    t = WatermarkTracker(['s'], progress.append)
    # Two owners pin the frontier at 40 while the source runs ahead to 100.
    t.hold('other', 40)
    t.hold('owner', 50)
    t.update_source('s', 100)
    assert progress == [40]
    # Replace owner's hold atomically. A buggy release-before-re-acquire would
    # temporarily leave only hold(40) — still fine here; push the check further:
    t.hold('owner', 45)
    assert progress == [40] and t.published == 40
    t.release('other')
    assert progress == [40, 45]  # owner's replacement hold is what remains
    t.hold('owner', 80)  # single jump straight to 80, no transient at 100
    assert progress == [40, 45, 80] and t.published == 80
    t.release('owner')
    assert progress == [40, 45, 80, 100]
    print('ok  replacing an owner hold never exposes a transient frontier')


def test_tracker_reject_hold_below_published():
    progress = []
    t = WatermarkTracker(['s'], progress.append)
    t.update_source('s', 100)
    assert progress == [100]
    try:
        t.hold('late', 99)
    except ValueError:
        pass
    else:
        raise AssertionError('hold below published frontier must be rejected')
    # State unchanged: owner absent, frontier unchanged.
    assert 'late' not in t._owners and t.published == 100
    # Equal to the published frontier is still legal (records at w are valid).
    t.hold('edge', 100)
    assert t.published == 100
    print('ok  hold below published rejected with state preserved; equal is allowed')


def test_tracker_monotone_sources():
    t = WatermarkTracker(['s'], lambda w: None)
    t.update_source('s', 10)
    try:
        t.update_source('s', 9)
    except ValueError:
        pass
    else:
        raise AssertionError('source regression must be rejected')
    try:
        t.update_source('unknown', 1)
    except ValueError:
        pass
    else:
        raise AssertionError('unknown source must be rejected')
    try:
        WatermarkTracker([], lambda w: None)
    except ValueError:
        pass
    else:
        raise AssertionError('empty source set must be rejected')
    print('ok  tracker source validation and monotonicity')


# --------------------------------------------------------------------------
# 3. BatchOperator / StrictSink
# --------------------------------------------------------------------------

def test_operator_normal():
    sink = StrictSink()
    op = BatchOperator(['source'], sink)
    op.schedule(('k', 'n', 't'), 10, 10, 'ordinary')
    op.advance('source', 10)
    assert sink.accepted == [[10, 'ordinary']] and sink.late == []
    # Record and equal watermark may appear in either order, but both exist.
    assert ['record', 10, 'ordinary'] in sink.events
    assert ['watermark', 10] in sink.events
    print('ok  operator normal delivery; record not late at equal watermark')


def test_operator_hold_protects_early_output():
    sink = StrictSink()
    op = BatchOperator(['source'], sink)
    op.schedule(('k', 'n', 't'), 90, 20, 'late-unless-held')
    op.advance('source', 100)
    assert sink.accepted == [[20, 'late-unless-held']], sink.accepted
    assert sink.late == [], sink.late
    # The exact downstream ordering: hold lets the frontier reach 20, then the
    # record is delivered, and only afterwards does 100 get published.
    assert sink.events == [
        ['watermark', 20],
        ['record', 20, 'late-unless-held'],
        ['watermark', 100],
    ], sink.events
    print('ok  deadline=90/output=20 with input wm 100: wm20 -> record20 -> wm100')


def test_operator_replacement_keeps_new_hold():
    sink = StrictSink()
    op = BatchOperator(['source'], sink)
    key = ('k', 'n', 't')
    op.schedule(key, 90, 20, 'old')
    op.schedule(key, 95, 30, 'new')  # replaces registration, fresh hold token
    op.advance('source', 100)
    assert sink.accepted == [[30, 'new']], sink.accepted
    assert sink.late == []
    # No watermark above 30 before the record at 30.
    wm100_index = sink.events.index(['watermark', 100])
    record_index = sink.events.index(['record', 30, 'new'])
    assert record_index < wm100_index
    print('ok  operator replacement: old hold retirement cannot drop new hold')


def test_operator_replacement_tombstone_holds_across_keys():
    # Replacing a timer before its heap entry is scanned must keep the old
    # registration's hold protected until an advance passes its deadline,
    # even though another identity drives the advance.
    sink = StrictSink()
    op = BatchOperator(['source'], sink)
    # key A: registered first at dl 10, then replaced by a far-future timer;
    # the old registration (hold at 10) becomes a tombstone.
    op.schedule(('kA', 'n', 't'), 10, 10, 'old-a')
    op.schedule(('kA', 'n', 't'), 100, 100, 'new-a')
    # key B: a live timer at output 50 pins the frontier in the meantime.
    op.schedule(('kB', 'n', 't'), 60, 50, 'b')
    op.advance('source', 40)
    # Tombstone deadline 10 has now been scanned past; its hold at 10 is
    # retired, but B's hold at 50 keeps the frontier at 50.
    assert sink.watermark == 40, sink.events
    op.advance('source', 70)
    assert sink.accepted == [[50, 'b']], sink.accepted
    assert sink.late == []
    op.advance('source', 100)
    assert sink.accepted == [[50, 'b'], [100, 'new-a']]
    assert sink.late == []
    assert op._tombstones == [] and op.frontier._owners == {}
    print('ok  replaced registration hold retires only after heap scan')


def test_operator_delete_releases_hold():
    sink = StrictSink()
    op = BatchOperator(['source'], sink)
    op.schedule(('k', 'n', 't'), 90, 20, 'v')
    op.delete(('k', 'n', 't'))
    assert op.frontier._owners == {}  # hold released promptly on delete
    op.advance('source', 100)
    assert sink.accepted == [] and sink.late == []
    assert sink.events[-1] == ['watermark', 100]
    assert not any(e[0] == 'record' for e in sink.events)
    print('ok  operator delete suppresses output and releases the hold')


def test_operator_callback_reregister_holds_continuously():
    sink = StrictSink()
    op = BatchOperator(['source'], sink)
    key = ('k', 'n', 't')

    def chain(operator):
        # Re-register the same identity from inside the firing callback.
        operator.schedule(key, 80, 40, 'second')

    op.schedule(key, 20, 20, 'first', after_emit=chain)
    op.advance('source', 50)
    assert sink.accepted == [[20, 'first']], sink.accepted
    assert sink.late == []
    # The new registration's hold at 40 must be live: frontier stuck at 40.
    assert sink.watermark == 40 and ['watermark', 100] not in sink.events
    op.advance('source', 100)
    assert sink.accepted == [[20, 'first'], [40, 'second']]
    assert sink.late == []
    assert sink.events[-1] == ['watermark', 100]
    print('ok  callback re-registration keeps an unbroken hold chain')


def test_operator_callback_deletes_due_neighbour():
    sink = StrictSink()
    op = BatchOperator(['source'], sink)

    def wipe(operator):
        operator.delete(('k', 'n', 'b'))

    op.schedule(('k', 'n', 'a'), 10, 10, 'a', after_emit=wipe)
    op.schedule(('k', 'n', 'b'), 10, 10, 'b')
    op.advance('source', 100)
    assert sink.accepted == [[10, 'a']], sink.accepted
    assert sink.late == []
    assert sink.events[-1] == ['watermark', 100]
    print('ok  operator callback deletes a due neighbour; its hold is released')


def test_operator_multiple_sources_min_frontier():
    sink = StrictSink()
    op = BatchOperator(['a', 'b'], sink)
    op.schedule(('k', 'n', 't'), 50, 50, 'v')
    op.advance('a', 100)
    assert sink.watermark == -1  # source b still -1
    op.advance('b', 60)
    assert sink.accepted == [[50, 'v']]
    assert sink.late == []
    print('ok  multiple sources: timers fire at the minimum source watermark')


def test_operator_replace_within_due_window_drains_hold():
    # Replacing an identity whose old deadline is already <= the current
    # timer watermark (due but not yet fired) must not leak the old hold.
    sink = StrictSink()
    op = BatchOperator(['source'], sink)
    key = ('k', 'n', 't')
    op.schedule(key, 10, 10, 'old')
    op.advance('source', 10)  # fires normally
    # Re-create, then replace again inside a later advance round via callback.
    events = []

    def replace_in_cb(operator):
        events.append('fired')
        operator.schedule(key, 50, 30, 'replacement')

    op.schedule(key, 20, 20, 'first', after_emit=replace_in_cb)
    op.advance('source', 20)
    assert events == ['fired']
    # The replacement is live; old registration's hold was released at fire.
    assert op.frontier._owners == {(key, 2): 30}
    op.advance('source', 50)
    assert sink.accepted == [[10, 'old'], [20, 'first'], [30, 'replacement']]
    assert sink.late == []
    assert op.frontier._owners == {} and op._tombstones == []
    print('ok  replacement in due window never leaks the retired hold')


def test_operator_with_external_tracker_users():
    # A non-timer producer uses a tracker independently.
    progress = []
    standalone = WatermarkTracker(['source'], progress.append)
    standalone.hold('buffer-producer', 25)

    sink = StrictSink()
    op = BatchOperator(['source'], sink)
    op.schedule(('k', 'n', 't'), 30, 30, 'v')
    # An extra external owner on the operator's own tracker.
    op.frontier.hold('external-buffer', 15)
    op.advance('source', 100)
    assert sink.accepted == [[30, 'v']] and sink.late == []
    assert sink.watermark == 15  # external hold still pins the frontier
    op.frontier.release('external-buffer')
    assert sink.watermark == 100
    # The standalone tracker is unaffected and still works.
    standalone.update_source('source', 100)
    assert standalone.published == 25
    standalone.release('buffer-producer')
    assert standalone.published == 100
    print('ok  external non-timer owners coexist with timer-held output')


# --------------------------------------------------------------------------
# 4. Differential fuzz: TimerService vs a trivial independent reference model
# --------------------------------------------------------------------------

class RefTimerService:
    """Plain-list implementation of exactly the same timer contract."""

    def __init__(self):
        self.watermark = -1
        self._seq = 0
        self._entries = {}  # identity -> [deadline, seq, output_ts, cb, live]

    def set(self, identity, deadline, output_timestamp, callback):
        if deadline <= self.watermark:
            raise ValueError('deadline')
        if output_timestamp > deadline:
            raise ValueError('output_ts')
        self._seq += 1
        self._entries[identity] = [deadline, self._seq, output_timestamp,
                                   callback, True]

    def delete(self, identity):
        e = self._entries.get(identity)
        if e is None or not e[4]:
            return False
        e[4] = False
        return True

    def advance(self, wm):
        if wm < self.watermark:
            raise ValueError('regression')
        self.watermark = wm
        while True:
            due = sorted(
                (e for e in self._entries.values() if e[4] and e[0] <= wm),
                key=lambda e: (e[0], e[1]),
            )
            if not due:
                return
            e = due[0]
            e[4] = False
            e[3](None, e[0], e[2])  # identity is unused by the fuzz callbacks


def fuzz_timers(seed):
    """Drive both models through an identical pre-generated operation plan."""
    rng = random.Random(seed)
    ids = [('k', 'ns', f't{i}') for i in range(6)]

    # Re-entrant callback actions are generated lazily via a factory keyed on
    # the service watermark that will be current when the callback fires
    # (the target watermark of the advance() round that makes it due).
    def gen_actions(wm_now, depth=0):
        actions = []
        for _ in range(rng.randint(0, 3 if depth < 2 else 0)):
            if rng.random() < 0.4:
                actions.append(('delete', rng.choice(ids)))
            else:
                ident = rng.choice(ids)
                dl = rng.randint(wm_now + 1, wm_now + 8)
                ots = rng.randint(wm_now, dl)
                tag = f'cb{rng.randint(0, 10**6)}'
                # Nested callback created here fires in some later round whose
                # watermark is at least dl; dl itself is therefore a legal bound.
                sub = gen_actions(dl, depth + 1)
                actions.append(('set', ident, dl, ots, tag, sub))
        return actions

    def gen_outer_plan():
        plan = []
        wm = -1
        for _ in range(150):
            c = rng.random()
            if c < 0.45:
                wm = rng.randint(wm + 1, wm + 6)
                plan.append(('advance', wm))
            elif c < 0.8:
                ident = rng.choice(ids)
                dl = rng.randint(wm + 1, wm + 10)
                ots = rng.randint(wm, dl)
                tag = f'o{rng.randint(0, 10**6)}'
                plan.append(('set', ident, dl, ots, tag))
            else:
                plan.append(('delete', rng.choice(ids)))
        return plan

    plan = gen_outer_plan()

    def run(model_cls):
        svc = model_cls()
        log = []
        # identity -> action list captured at set() time; both models use the
        # same RNG-derived plan because generation happens inside identical
        # callback call sequences on each model run.
        def make_cb(tag):
            def cb(identity, deadline, ots):
                log.append(('fire', deadline, ots, tag))
                for action in gen_actions(svc.watermark):
                    if action[0] == 'delete':
                        svc.delete(action[1])
                    else:
                        _, ident, dl, t, sub_tag, sub = action
                        svc.set(ident, dl, t, make_cb(sub_tag))
            return cb

        for op in plan:
            if op[0] == 'advance':
                svc.advance(op[1])
            elif op[0] == 'delete':
                svc.delete(op[1])
            else:
                _, ident, dl, ots, tag = op
                svc.set(ident, dl, ots, make_cb(tag))
        svc.advance(1000)
        return svc, log

    # The two runs must produce identical action sequences: generation uses
    # the same RNG algorithm seeded identically and is driven by the same
    # deterministic firing order, so draw sequences match one-to-one.
    rng.seed(seed)
    real_svc, real_log = run(TimerService)
    rng.seed(seed)
    ref_svc, ref_log = run(RefTimerService)
    assert real_svc.watermark == ref_svc.watermark == 1000
    assert real_log == ref_log, (seed, real_log, ref_log)


# --------------------------------------------------------------------------
# 5. End-to-end operator fuzz: StrictSink must never see a late record
# --------------------------------------------------------------------------

def fuzz_operator(seed):
    rng = random.Random(seed)
    sink = StrictSink()
    op = BatchOperator(['s1', 's2'], sink)
    ids = [('k', 'ns', f't{i}') for i in range(5)]
    source_wm = {'s1': -1, 's2': -1}

    for step in range(150):
        c = rng.random()
        if c < 0.5:
            src = rng.choice(['s1', 's2'])
            source_wm[src] = rng.randint(source_wm[src], source_wm[src] + 12)
            op.advance(src, source_wm[src])
        elif c < 0.85:
            ident = rng.choice(ids)
            op.delete(ident)
        else:
            ident = rng.choice(ids)
            input_min = min(source_wm.values())
            dl = rng.randint(input_min + 1, input_min + 15)
            ots = rng.randint(max(0, input_min - 5), dl)
            if ots < op.frontier.published:
                continue  # contract: outer schedules stay in the legal range
            do_chain = rng.random() < 0.3

            def make_after(r):
                def after(operator):
                    if r.random() >= 0.5:
                        return
                    base = min(operator.frontier.sources.values())
                    dl2 = r.randint(max(operator.frontier.published, base) + 1,
                                    max(operator.frontier.published, base) + 10)
                    ots2 = r.randint(operator.frontier.published, dl2)
                    operator.schedule(ident, dl2, ots2, f'chained-{ots2}')
                return after

            op.schedule(ident, dl, ots, f'v-{ots}',
                        after_emit=make_after(rng) if do_chain else None)

        # Invariants after every outer operation.
        # 1. StrictSink never classifies anything as late.
        assert sink.late == [], (seed, step, sink.late, sink.events)
        # 2. Sink watermark tracks the tracker's published frontier exactly.
        assert sink.watermark == op.frontier.published
        # 3. No record was ever preceded by a watermark above its timestamp.
        for i, e in enumerate(sink.events):
            if e[0] == 'record':
                assert all(w[1] <= e[1]
                           for w in sink.events[:i] if w[0] == 'watermark'), \
                    (seed, sink.events)

    # Drain everything: chained callbacks can schedule ever-later timers and
    # replacements leave tombstones, so keep advancing until both are empty.
    drain = max(source_wm.values()) + 1
    for _ in range(100):
        if op.timers._active:
            drain = max(drain, max(e[0] for e in op.timers._active.values()) + 1)
        if op._tombstones:
            drain = max(drain, max(d for d, _ in op._tombstones) + 1)
        op.advance('s1', drain)
        op.advance('s2', drain)
        if not op.timers._active and not op._tombstones:
            break
    terminal = drain
    assert sink.late == [], sink.late
    assert sink.watermark == terminal
    assert op.timers._active == {} and op.frontier._owners == {}
    assert op._tokens == {} and op._tombstones == []
    assert sink.events[-1] == ['watermark', terminal]


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
    for seed in range(300):
        fuzz_timers(seed)
    print('ok  timer differential fuzz (300 seeds vs independent reference model)')
    for seed in range(200):
        fuzz_operator(seed)
    print('ok  operator fuzz (200 seeds): no late records, invariants hold')
    print('\nALL CHECKS PASSED')


if __name__ == '__main__':
    main()
