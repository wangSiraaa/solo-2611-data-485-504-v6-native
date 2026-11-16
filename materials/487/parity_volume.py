"""A rotating-parity three-file volume with a public logical-block interface."""
from pathlib import Path
from block_journal import BlockJournal

BLOCK = 16


def xor_bytes(*items):
    result = bytearray(BLOCK)
    for item in items:
        for index, value in enumerate(item):
            result[index] ^= value
    return bytes(result)


class ParityVolume:
    def __init__(self, root, stripes=4):
        self.root = Path(root)
        self.stripes = stripes
        self.journal = BlockJournal(root)

    @staticmethod
    def location(logical_block):
        stripe, position = divmod(logical_block, 2)
        parity = stripe % 3
        data_members = [member for member in range(3) if member != parity]
        return stripe, data_members[position], parity

    def _block(self, member, stripe):
        with (self.root / f'member-{member}.bin').open('rb') as stream:
            stream.seek(stripe * BLOCK)
            result = stream.read(BLOCK)
        if len(result) != BLOCK:
            raise ValueError('short member')
        return result

    def read(self, logical_block):
        stripe, member, parity = self.location(logical_block)
        return self._block(member, stripe)

    def write(self, request_id, logical_block, data, after_write=None):
        if len(data) != BLOCK:
            raise ValueError('one full logical block required')
        stripe, member, parity = self.location(logical_block)
        # Originally only the zero-initialized volume was written.
        new_parity = xor_bytes(data)
        self.journal.apply(request_id, [
            (f'member-{member}.bin', stripe * BLOCK, data),
            (f'member-{parity}.bin', stripe * BLOCK, new_parity),
        ], after_write)

    def recover(self):
        return self.journal.recover()

    def rebuild(self, missing_member):
        target = self.root / f'member-{missing_member}.bin'
        with target.open('wb') as stream:
            for stripe in range(self.stripes):
                stream.write(xor_bytes(*(self._block(member, stripe)
                    for member in range(3) if member != missing_member)))
        return target

