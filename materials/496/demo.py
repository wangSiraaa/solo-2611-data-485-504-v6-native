from pathlib import Path
from tempfile import TemporaryDirectory
from quota import Quota
from volume import Volume

with TemporaryDirectory() as directory:
    q = Quota(8)
    v = Volume(Path(directory) / 'blocks', q)
    v.create('parent', b'A' * 16 + b'B' * 16)
    v.clone('parent', 'child')
    v.write('child', 0, b'hi')
    print(v.read('parent').decode(), v.read('child').decode(), q.stats())

