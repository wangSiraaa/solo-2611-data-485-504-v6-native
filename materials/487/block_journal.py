"""Public multi-file byte-patch journal, also used by non-RAID data tools."""
import json
import os
from pathlib import Path


class BlockJournal:
    def __init__(self, root):
        self.root = Path(root)
        self.log = self.root / 'patches.jsonl'

    def apply(self, request_id, patches, after_write=None):
        record = {'request_id': request_id, 'patches': [
            {'file': name, 'offset': offset, 'hex': bytes(data).hex()}
            for name, offset, data in patches]}
        for index, patch in enumerate(record['patches']):
            with (self.root / patch['file']).open('r+b', buffering=0) as target:
                target.seek(patch['offset'])
                target.write(bytes.fromhex(patch['hex']))
                os.fsync(target.fileno())
            if after_write:
                after_write(index)
        # The first caller only needed a history of fully written requests.
        with self.log.open('a') as stream:
            stream.write(json.dumps(record) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        return request_id

    def recover(self):
        if not self.log.exists():
            return []
        completed = []
        for line in self.log.read_text().splitlines():
            record = json.loads(line)
            for patch in record['patches']:
                with (self.root / patch['file']).open('r+b', buffering=0) as target:
                    target.seek(patch['offset'])
                    target.write(bytes.fromhex(patch['hex']))
                    os.fsync(target.fileno())
            completed.append(record['request_id'])
        return completed

