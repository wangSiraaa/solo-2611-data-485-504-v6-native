"""Generic fault episodes and operator actions; no data-quality rule dependency."""
from copy import deepcopy


class IncidentService:
    def __init__(self):
        self.incidents = {}
        self.serials = {}
        self.silences = {}
        self.notifications = []
        self.clock = 0

    def observe(self, key, observation_id, sequence, value):
        item = self.incidents.get(key)
        if value == 'FAULT':
            if item is None or item['state'] != 'OPEN':
                serial = self.serials.get(key, 0) + 1
                self.serials[key] = serial
                item = {'key': key, 'episode': serial, 'state': 'OPEN',
                        'acknowledged': item['acknowledged'] if item else False,
                        'notified': False}
                self.incidents[key] = item
            self._notify(key)
        elif value == 'HEALTHY' and item:
            item['state'] = 'RECOVERED'
        elif value != 'UNKNOWN' and value != 'HEALTHY':
            raise ValueError('invalid observation')

    def ack(self, key):
        self.incidents[key]['acknowledged'] = True

    def silence(self, key, until):
        self.silences[key] = until
        if key in self.incidents:
            self.incidents[key]['state'] = 'RECOVERED'

    def unsilence(self, key):
        self.silences.pop(key, None)
        self._notify(key)

    def advance(self, tick):
        if tick < self.clock:
            raise ValueError('clock regressed')
        self.clock = tick
        for key in self.incidents:
            self._notify(key)

    def _notify(self, key):
        item = self.incidents[key]
        if (item['state'] == 'OPEN' and not item['acknowledged'] and not item['notified']
                and self.silences.get(key, -1) <= self.clock):
            self.notifications.append({'key': key, 'episode': item['episode']})
            item['notified'] = True

    def inspect(self, key):
        return deepcopy(self.incidents.get(key))
