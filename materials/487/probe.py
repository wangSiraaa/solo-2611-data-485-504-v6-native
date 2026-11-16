"""Real files; logical expected data and XOR parity are built independently."""
import json
from pathlib import Path
import tempfile
from block_journal import BlockJournal
from parity_volume import ParityVolume, BLOCK


def initialize(root, values):
    members = [bytearray(BLOCK * 4) for _ in range(3)]
    for stripe in range(4):
        parity = stripe % 3
        data_members = [index for index in range(3) if index != parity]
        a, b = values[2 * stripe: 2 * stripe + 2]
        for member, value in zip(data_members, [a, b]):
            members[member][stripe*BLOCK:(stripe+1)*BLOCK] = bytes([value])*BLOCK
        members[parity][stripe*BLOCK:(stripe+1)*BLOCK] = bytes([a ^ b])*BLOCK
    for index, data in enumerate(members):
        (root / f'member-{index}.bin').write_bytes(data)


def crash_after_first(index):
    if index == 0:
        raise RuntimeError('injected stop after a real file patch fsync')


observations = {}
with tempfile.TemporaryDirectory() as folder:
    base = Path(folder)
    generic = base / 'generic'
    generic.mkdir()
    (generic/'a.bin').write_bytes(b'old-a')
    (generic/'b.bin').write_bytes(b'old-b')
    journal = BlockJournal(generic)
    journal.apply('normal', [('a.bin',0,b'new-a'),('b.bin',0,b'new-b')])
    observations['generic_normal'] = [(generic/name).read_bytes().decode() for name in ['a.bin','b.bin']]
    try:
        journal.apply('interrupted', [('a.bin',0,b'nexta'),('b.bin',0,b'nextb')], crash_after_first)
    except RuntimeError:
        pass
    recovered = BlockJournal(generic).recover()
    observations['generic_recovery'] = {'rows': [(generic/name).read_bytes().decode() for name in ['a.bin','b.bin']],
        'durable_completed': recovered,
        'expected': 'either [new-a,new-b] or [nexta,nextb], never mixed'}
    # Replaying an older complete record happens to hide that interrupted write;
    # a fresh generic journal has no earlier payload to restore from.
    isolated = base / 'isolated'
    isolated.mkdir()
    (isolated/'a.bin').write_bytes(b'old-a')
    (isolated/'b.bin').write_bytes(b'old-b')
    try:
        BlockJournal(isolated).apply('one', [('a.bin',0,b'new-a'),('b.bin',0,b'new-b')], crash_after_first)
    except RuntimeError:
        pass
    BlockJournal(isolated).recover()
    observations['generic_independent_failure'] = {'actual': [(isolated/name).read_bytes().decode() for name in ['a.bin','b.bin']],
        'allowed_complete_states': [['old-a','old-b'],['new-a','new-b']]}
    normal = base/'normal';normal.mkdir();initialize(normal,[0]*8)
    volume = ParityVolume(normal)
    volume.write('zero-first', 0, b'\x07'*BLOCK)
    observations['volume_normal'] = {'value': volume.read(0).hex(), 'expected': (b'\x07'*BLOCK).hex()}
    partial = base/'partial';partial.mkdir();initialize(partial,[3,5,7,9,11,13,15,17])
    volume = ParityVolume(partial)
    volume.write('partial', 1, b'\x08'*BLOCK)
    actual_parity = (partial/'member-0.bin').read_bytes()[:BLOCK]
    (partial/'member-1.bin').unlink()
    try:
        degraded = {'value': volume.read(0).hex()}
    except Exception as error:
        degraded = {'error': type(error).__name__}
    volume.rebuild(1)
    observations['parity_with_correct_generic_commit'] = {'actual_parity': actual_parity.hex(),
        'expected_parity': (b'\x0b'*BLOCK).hex(),
        'degraded_read': degraded, 'expected_logical_zero': (b'\x03'*BLOCK).hex(),
        'rebuilt_logical_zero': volume.read(0).hex()}
    hole = base/'write-hole';hole.mkdir();initialize(hole,[3,5,7,9,11,13,15,17])
    volume = ParityVolume(hole)
    try:
        volume.write('interrupted', 0, b'\x06'*BLOCK, crash_after_first)
    except RuntimeError:
        pass
    ParityVolume(hole).recover()
    physical = [(hole/f'member-{i}.bin').read_bytes()[:BLOCK][0] for i in range(3)]
    observations['real_partial_stripe_interruption'] = {'physical_first_bytes': physical,
        'actual_parity_consistent': physical[0] == (physical[1]^physical[2]),
        'allowed_logical_complete_states': [[3,5],[6,5]]}
print(json.dumps(observations, indent=2))
