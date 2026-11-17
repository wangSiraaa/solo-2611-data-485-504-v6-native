"""Generic fault episodes and operator actions; no data-quality rule dependency.

Observations
------------
An observation is ``(key, observation_id, sequence, value)`` where ``value``
is ``FAULT``, ``HEALTHY`` or ``UNKNOWN``.  Its canonical content is exactly
those four fields: JSON object key order does not affect identity, but the
four values do.  ``(key, observation_id)`` is the identity; re-sending it with
the same content returns the original result and changes nothing, while a
different content under the same identity is rejected and the original record
kept.

Per key, each ``sequence`` belongs to one observation id (reuse is rejected)
and only an observation whose ``sequence`` exceeds the key's frontier applies
its value.  A smaller, late observation is retained as its own evidence but
never rewrites the current episode, a fault decision, acknowledgement or
notification.

Fault episodes
--------------
For a key with no OPEN episode a ``FAULT`` opens a brand-new episode (new
identity, fresh unacknowledged/unnotified flags).  A ``FAULT`` on the already
OPEN episode keeps that episode.  ``HEALTHY`` moves the OPEN episode to
``RECOVERED``; ``UNKNOWN`` leaves the fault state unchanged.  A ``FAULT`` after
recovery therefore always creates a new identity and never carries the old
ACK into the new fault.

Operator actions
----------------
``ack`` only acknowledges the current OPEN episode (rejected when nothing is
OPEN) and does not change whether a fault exists.  ``silence(key, until)``
suppresses notifications for that key until ``until`` and may be set before
any fault exists or replace a previous deadline; it never recovers an OPEN
episode.  ``advance(tick)`` moves a non-decreasing logical clock and expires
silences at ``tick >= until``; ``unsilence`` releases one early.

Notification record
-------------------
At most one notification is recorded per episode: when the episode is OPEN,
unacknowledged, unnotified and not silenced.  A notification suppressed at
episode creation is recorded once later when the silence is released or
expires and the episode is still eligible.  Repeat observations and clock
advances never notify an already-notified episode, recovery never backfills an
old fault, and acknowledgement never revokes a recorded notification.  This is
a local record only; no real pager or mail is contacted.
"""
from copy import deepcopy
import json


class IncidentError(ValueError):
    """Invalid observation or illegal operator action; nothing is changed."""


def canonical_text(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _is_int(value):
    return type(value) is int  # reject bool


def _require_key(key):
    if not isinstance(key, str) or not key:
        raise IncidentError('key must be a non-empty string')


class IncidentService:
    def __init__(self):
        # key -> frontier / sequence ownership / episodes / current episode id
        self._keys = {}
        self._observations = {}      # (key, observation_id) -> stored result
        self._observation_text = {}  # (key, observation_id) -> canonical content
        self._silences = {}          # key -> until
        self._notifications = []     # local notification records
        self._notification_serial = 0
        self._clock = 0

    # ------------------------------------------------------------- observations
    def observe(self, key, observation_id, sequence, value):
        """Apply (or retain, if late) one generic fault observation."""
        _require_key(key)
        if not isinstance(observation_id, str) or not observation_id:
            raise IncidentError('observation_id must be a non-empty string')
        if not _is_int(sequence) or sequence <= 0:
            raise IncidentError('sequence must be a positive integer')
        if value not in ('FAULT', 'HEALTHY', 'UNKNOWN'):
            raise IncidentError('value must be FAULT, HEALTHY or UNKNOWN')

        content = {'key': key, 'observation_id': observation_id,
                   'sequence': sequence, 'value': value}
        identity = (key, observation_id)
        text = canonical_text(content)

        stored_text = self._observation_text.get(identity)
        if stored_text is not None:
            if stored_text != text:
                raise IncidentError(
                    'observation identity conflict for key=%r id=%r'
                    % (key, observation_id))
            return deepcopy(self._observations[identity])

        state = self._keys.get(key)
        if state is None:
            state = {'frontier': 0, 'owners': {}, 'episodes': {}, 'current': None}

        occupant = state['owners'].get(sequence)
        if occupant is not None and occupant != observation_id:
            raise IncidentError(
                'sequence %d is already used by observation %r on key %r'
                % (sequence, occupant, key))

        applied = sequence > state['frontier']
        if applied:
            state['frontier'] = sequence
            state['owners'][sequence] = observation_id
            self._apply_value(state, key, sequence, value)
            self._maybe_notify(state, key)
        else:
            # Late observation: retain evidence, change no decision.
            state['owners'][sequence] = observation_id

        episode = self._current_episode(state) if applied else None
        record = {
            'key': key,
            'observation_id': observation_id,
            'sequence': sequence,
            'value': value,
            'applied': applied,
            'episode': episode['episode'] if episode else None,
            'state': episode['state'] if episode else None,
            'acknowledged': episode['acknowledged'] if episode else False,
            'notified': episode['notified'] if episode else False,
            'silenced': self._is_silenced(key),
        }
        self._keys[key] = state
        self._observation_text[identity] = text
        self._observations[identity] = deepcopy(record)
        return deepcopy(record)

    def _apply_value(self, state, key, sequence, value):
        current = self._current_episode(state)
        if value == 'FAULT':
            if current is None or current['state'] != 'OPEN':
                serial = len(state['episodes']) + 1
                current = {
                    'key': key, 'episode': serial, 'state': 'OPEN',
                    'acknowledged': False, 'notified': False,
                    'opened_sequence': sequence, 'opened_clock': self._clock,
                }
                state['episodes'][serial] = current
                state['current'] = serial
        elif value == 'HEALTHY':
            if current is not None and current['state'] == 'OPEN':
                current['state'] = 'RECOVERED'
                current['recovered_sequence'] = sequence
                current['recovered_clock'] = self._clock
        # UNKNOWN leaves the fault state untouched.

    @staticmethod
    def _current_episode(state):
        if not state or state['current'] is None:
            return None
        return state['episodes'][state['current']]

    # ------------------------------------------------------------- operator API
    def ack(self, key):
        """Acknowledge the current OPEN episode; reject when none is OPEN."""
        _require_key(key)
        state = self._keys.get(key)
        episode = self._current_episode(state)
        if episode is None or episode['state'] != 'OPEN':
            raise IncidentError('no OPEN fault to acknowledge')
        episode['acknowledged'] = True
        return self._snapshot(key)

    def silence(self, key, until):
        """Suppress notifications for this key until ``until > clock``."""
        _require_key(key)
        if not _is_int(until):
            raise IncidentError('until must be an integer')
        if until <= self._clock:
            raise IncidentError('until must be greater than the current clock')
        self._silences[key] = until
        return self._snapshot(key)

    def unsilence(self, key):
        """Release a silence early; repeating it is a no-op."""
        _require_key(key)
        self._silences.pop(key, None)
        state = self._keys.get(key)
        if state is not None:
            self._maybe_notify(state, key)
        return self._snapshot(key)

    def advance(self, tick):
        """Move the non-decreasing logical clock and expire due silences."""
        if not _is_int(tick):
            raise IncidentError('tick must be an integer')
        if tick < self._clock:
            raise IncidentError('clock must not regress')
        self._clock = tick
        for key, until in list(self._silences.items()):
            if tick >= until:
                del self._silences[key]
        for key in sorted(self._keys):
            self._maybe_notify(self._keys[key], key)
        return {key: self._snapshot(key) for key in sorted(self._keys)}

    # -------------------------------------------------------------- notifications
    def _is_silenced(self, key):
        until = self._silences.get(key)
        return until is not None and self._clock < until

    def _maybe_notify(self, state, key):
        if state is None or key is None:
            return
        episode = self._current_episode(state)
        if episode is None or episode['state'] != 'OPEN':
            return
        if episode['acknowledged'] or episode['notified']:
            return
        if self._is_silenced(key):
            return
        self._notification_serial += 1
        episode['notified'] = True
        self._notifications.append({
            'notification_id': self._notification_serial,
            'key': key,
            'episode': episode['episode'],
            'clock': self._clock,
        })

    # ------------------------------------------------------------------- queries
    def _snapshot(self, key):
        until = self._silences.get(key)
        state = self._keys.get(key)
        episode = self._current_episode(state)
        return {
            'key': key,
            'open': bool(episode is not None and episode['state'] == 'OPEN'),
            'episode': episode['episode'] if episode else None,
            'state': episode['state'] if episode else None,
            'acknowledged': bool(episode and episode['acknowledged']),
            'notified': bool(episode and episode['notified']),
            'silenced': until is not None and self._clock < until,
            'silence_until': until,
            'clock': self._clock,
        }

    def inspect(self, key):
        """Return an immutable snapshot of the key, or None if fully unknown."""
        _require_key(key)
        if key not in self._keys and key not in self._silences:
            return None
        return deepcopy(self._snapshot(key))

    def episodes(self, key):
        """Return every episode identity ever created for this key."""
        _require_key(key)
        state = self._keys.get(key)
        if state is None:
            return []
        return [deepcopy(state['episodes'][serial])
                for serial in sorted(state['episodes'])]

    def notification_records(self, key=None):
        """Return local notification records (optionally for one key)."""
        return [deepcopy(item) for item in self._notifications
                if key is None or item['key'] == key]

    def observations(self, key):
        """Return retained observation evidence for this key."""
        _require_key(key)
        return [deepcopy(self._observations[identity])
                for identity in sorted(self._observations, key=lambda item: item[1])
                if identity[0] == key]

    # ------------------------------------------------- compatibility read views
    @property
    def clock(self):
        return self._clock

    @property
    def silences(self):
        return dict(self._silences)

    @property
    def incidents(self):
        return {key: self._snapshot(key) for key in self._keys}

    @property
    def serials(self):
        """Read-only count of episodes created per key."""
        return {key: len(state['episodes']) for key, state in self._keys.items()}

    @property
    def notifications(self):
        """Read-only copy of local notification records."""
        return self.notification_records()
