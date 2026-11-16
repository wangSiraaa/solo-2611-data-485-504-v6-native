"""Small writable views over real fixed-size block files."""
from pathlib import Path
import uuid

BLOCK_SIZE = 16


class Volume:
    def __init__(self, directory, quota):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.quota = quota
        self.views = {}

    def _new_block(self, data):
        block_id = uuid.uuid4().hex
        (self.directory / block_id).write_bytes(data)
        return block_id

    def _new_name(self, name):
        if not isinstance(name, str) or not name or name in self.views or len(self.views) >= 4:
            raise ValueError('view name or view limit')

    def create(self, name, data):
        self._new_name(name)
        if not isinstance(data, bytes) or not len(data) or len(data) % BLOCK_SIZE or len(data) > 4 * BLOCK_SIZE:
            raise ValueError('whole blocks required')
        token = self.quota.reserve(len(data) // BLOCK_SIZE)
        ids = [self._new_block(data[i:i + BLOCK_SIZE]) for i in range(0, len(data), BLOCK_SIZE)]
        self.quota.commit(token, ids)
        self.views[name] = ids

    def clone(self, source, target):
        self._new_name(target)
        self.views[target] = list(self.views[source])

    def layout(self):
        return {name: list(ids) for name, ids in self.views.items()}

    def read(self, name, offset=0, length=None):
        data = b''.join((self.directory / x).read_bytes() for x in self.views[name])
        length = len(data) - offset if length is None else length
        if type(offset) is not int or type(length) is not int or offset < 0 or length < 0 or offset + length > len(data):
            raise ValueError('range')
        return data[offset:offset + length]

    def _refs(self, block_id):
        return sum(ids.count(block_id) for ids in self.views.values())

    def write(self, name, offset, payload, fault=None):
        ids = self.views[name]
        if (not isinstance(payload, bytes) or type(offset) is not int
                or offset < 0 or offset + len(payload) > len(ids) * BLOCK_SIZE):
            raise ValueError('range or payload')
        if not payload:
            return
        first, last = offset // BLOCK_SIZE, (offset + len(payload) - 1) // BLOCK_SIZE
        for ordinal, index in enumerate(range(first, last + 1), 1):
            old_id = ids[index]
            old = bytearray((self.directory / old_id).read_bytes())
            start, end = max(offset, index * BLOCK_SIZE), min(offset + len(payload), (index + 1) * BLOCK_SIZE)
            old[start - index * BLOCK_SIZE:end - index * BLOCK_SIZE] = payload[start - offset:end - offset]
            if self._refs(old_id) > 1:
                token = self.quota.reserve(1)
                new_id = self._new_block(bytes(old))
                self.quota.commit(token, [new_id])
                ids[index] = new_id
            else:
                (self.directory / old_id).write_bytes(old)
            if fault is not None:
                fault('after_block_written', ordinal)

    def drop(self, name):
        ids = self.views.pop(name)
        for block_id in ids:
            if self._refs(block_id) == 0:
                (self.directory / block_id).unlink()
                self.quota.free([block_id])

