"""Physical allocation account used by the volume and other local clients."""
import uuid


class NoSpace(Exception):
    pass


class Quota:
    def __init__(self, capacity):
        if type(capacity) is not int or not 1 <= capacity <= 16:
            raise ValueError('capacity')
        self.capacity = capacity
        self.held = {}
        self.allocated = set()

    def reserve(self, count):
        if type(count) is not int or count <= 0:
            raise ValueError('count')
        if len(self.allocated) + count > self.capacity:
            raise NoSpace('physical capacity')
        token = uuid.uuid4().hex
        self.held[token] = count
        return token

    def commit(self, token, block_ids):
        ids = list(block_ids)
        if (token not in self.held or len(ids) != len(set(ids))
                or any(not isinstance(x, str) or not x for x in ids)
                or len(ids) > self.held[token] or set(ids) & self.allocated):
            raise ValueError('invalid allocation commit')
        self.allocated.update(ids)
        del self.held[token]

    def cancel(self, token):
        if token not in self.held:
            raise ValueError('unknown reservation')
        del self.held[token]

    def free(self, block_ids):
        ids = list(block_ids)
        if len(ids) != len(set(ids)) or not set(ids) <= self.allocated:
            raise ValueError('invalid allocation free')
        self.allocated.difference_update(ids)

    def stats(self):
        used, held = len(self.allocated), sum(self.held.values())
        return dict(capacity=self.capacity, used=used, held=held,
                    free=self.capacity - used - held)

