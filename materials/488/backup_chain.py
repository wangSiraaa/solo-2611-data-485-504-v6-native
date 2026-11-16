"""Base/delta products and an independent restore entry, including imported chains."""
import hashlib
import json
from pathlib import Path
from changed_blocks import COUNT


def digest(data):
    return hashlib.sha256(data).hexdigest()


class BackupChain:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)

    def read_manifest(self, backup_id):
        return json.loads((self.root / (backup_id + '.json')).read_text())

    def create_base(self, backup_id, tracker):
        snapshot = tracker.freeze()
        blocks = {str(block): tracker.read_snapshot(snapshot, block).hex() for block in range(COUNT)}
        record = {'id': backup_id, 'kind': 'base', 'parent': None,
            'image_id': snapshot['image_id'], 'base_generation': 0,
            'end_generation': snapshot['end_generation'], 'blocks': blocks,
            'target_sha256': digest(b''.join(bytes.fromhex(blocks[str(i)]) for i in range(COUNT)))}
        (self.root / (backup_id + '.json')).write_text(json.dumps(record))
        tracker.ack(snapshot)
        return record

    def create_delta(self, backup_id, parent_id, tracker, snapshot=None):
        snapshot = tracker.freeze() if snapshot is None else snapshot
        blocks = {str(block): tracker.read_snapshot(snapshot, block).hex() for block in snapshot['changed']}
        data = bytearray(self.restore(parent_id))
        for block, payload in blocks.items():
            start = int(block)*16
            data[start:start+16] = bytes.fromhex(payload)
        record = {'id': backup_id, 'kind': 'delta', 'parent': parent_id,
            'image_id': snapshot['image_id'], 'base_generation': snapshot['base_generation'],
            'end_generation': snapshot['end_generation'], 'blocks': blocks,
            'target_sha256': digest(data)}
        (self.root / (backup_id + '.json')).write_text(json.dumps(record))
        tracker.ack(snapshot)
        return record

    def restore(self, backup_id):
        record = self.read_manifest(backup_id)
        if record['kind'] == 'base':
            data = bytearray(16*COUNT)
        else:
            data = bytearray(self.restore(record['parent']))
        for block, payload in record['blocks'].items():
            start = int(block)*16
            data[start:start+16] = bytes.fromhex(payload)
        return bytes(data)

