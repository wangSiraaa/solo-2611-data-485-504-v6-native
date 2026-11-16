"""Changed-block tracking used by mirror clients independently of backup chains."""
import os
from pathlib import Path

BLOCK = 16
COUNT = 16


class BlockIO:
    def __init__(self, path):
        self.path = Path(path)
        self.read_log = []

    def read(self, block):
        with self.path.open('rb') as stream:
            stream.seek(block * BLOCK)
            data = stream.read(BLOCK)
        self.read_log.append(block)
        if len(data) != BLOCK:
            raise ValueError('short source block')
        return data

    def write(self, block, data):
        with self.path.open('r+b', buffering=0) as stream:
            stream.seek(block * BLOCK)
            stream.write(data)
            os.fsync(stream.fileno())


class ChangedBlocks:
    def __init__(self, source, image_id):
        self.source = source
        self.image_id = image_id
        self.generation = 0
        self.confirmed = 0
        self.dirty = set()
        self.next_token = 1

    def write(self, block, data):
        if not 0 <= block < COUNT or len(data) != BLOCK:
            raise ValueError('invalid block mutation')
        self.source.write(block, data)
        self.generation += 1
        self.dirty.add(block)

    def freeze(self):
        snapshot = {'token': self.next_token, 'image_id': self.image_id,
                    'base_generation': self.confirmed, 'end_generation': self.generation,
                    'changed': self.dirty}
        self.next_token += 1
        return snapshot

    def read_snapshot(self, snapshot, block):
        return self.source.read(block)

    def ack(self, snapshot):
        self.confirmed = snapshot['end_generation']
        self.dirty.clear()

