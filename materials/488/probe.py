import hashlib
import json
from pathlib import Path
import tempfile
from changed_blocks import BlockIO, ChangedBlocks, BLOCK, COUNT
from backup_chain import BackupChain


def fresh(root, image_id, value=1):
    path = root / (image_id+'.bin')
    path.write_bytes(bytes([value])*(BLOCK*COUNT))
    io = BlockIO(path)
    return ChangedBlocks(io, image_id)


out = {}
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    direct = fresh(root, 'direct')
    direct.write(0, b'\x02'*BLOCK)
    one = direct.freeze()
    out['direct_normal'] = {'changed': sorted(one['changed']), 'block0': direct.read_snapshot(one,0).hex()}
    direct.ack(one)
    out['direct_normal']['after_ack_changed'] = sorted(direct.freeze()['changed'])
    direct.write(1, b'\x03'*BLOCK)
    frozen = direct.freeze()
    direct.write(1, b'\x04'*BLOCK)
    direct.write(2, b'\x05'*BLOCK)
    before_ack = {'frozen_changed': sorted(frozen['changed']), 'frozen_block1': direct.read_snapshot(frozen,1).hex()}
    direct.ack(frozen)
    out['direct_freeze_and_ack'] = {'actual': before_ack,
        'after_ack_dirty': sorted(direct.freeze()['changed']),
        'expected_frozen_changed': [1], 'expected_frozen_block1': (b'\x03'*BLOCK).hex(),
        'expected_after_ack_dirty': [1,2]}
    tracker = fresh(root, 'normal-image')
    chain = BackupChain(root/'chain')
    chain.create_base('base-a',tracker)
    tracker.source.read_log.clear()
    tracker.write(3,b'\x07'*BLOCK)
    tracker.write(5,b'\x08'*BLOCK)
    chain.create_delta('delta-a','base-a',tracker)
    restored = chain.restore('delta-a')
    expected = bytearray(b'\x01'*(BLOCK*COUNT));expected[48:64]=b'\x07'*BLOCK;expected[80:96]=b'\x08'*BLOCK
    out['normal_base_delta_restore'] = {'actual_sha256': hashlib.sha256(restored).hexdigest(),
        'expected_sha256': hashlib.sha256(expected).hexdigest(),
        'actual_source_payload_blocks': len(tracker.source.read_log),
        'source_read_block_ids': tracker.source.read_log[:], 'maximum_source_payload_blocks':4}
    other = fresh(root,'other-image',9)
    chain.create_base('base-b',other)
    bad = chain.read_manifest('delta-a');bad['id']='wrong-parent';bad['parent']='base-b'
    (chain.root/'wrong-parent.json').write_text(json.dumps(bad))
    wrong = chain.restore('wrong-parent')
    out['imported_wrong_ancestor'] = {'actual_status':'returned-bytes',
        'actual_sha256':hashlib.sha256(wrong).hexdigest(),
        'declared_target_sha256':bad['target_sha256'],
        'expected':'reject image identity/base generation/parent or final digest mismatch'}
    corrupt = chain.read_manifest('delta-a');corrupt['id']='corrupt';corrupt['blocks']['3']='00'*BLOCK
    (chain.root/'corrupt.json').write_text(json.dumps(corrupt))
    out['imported_corrupt_payload'] = {'returned_first_changed_block':chain.restore('corrupt')[48:64].hex(),
        'expected':'reject payload/target digest mismatch'}
    tracker = fresh(root, 'coupled')
    chain2 = BackupChain(root/'coupled-chain');chain2.create_base('b0',tracker)
    tracker.write(0,b'\x02'*BLOCK);snap=tracker.freeze()
    tracker.write(0,b'\x03'*BLOCK)
    chain2.create_delta('b1','b0',tracker,snap)
    tracker.source.path.unlink()
    out['actual_backup_point_after_later_write'] = {'restored_block0':chain2.restore('b1')[:BLOCK].hex(),
        'expected_at_freeze':(b'\x02'*BLOCK).hex(),
        'expected_new_write_still_dirty':[0], 'actual_after_old_ack_dirty':sorted(tracker.freeze()['changed'])}
print(json.dumps(out,indent=2))
