from pathlib import Path
import json
import tempfile
from store import Store
import batch

with tempfile.TemporaryDirectory(prefix='conditional-import-') as folder:
    store = Store(Path(folder) / 'work.db')
    first = store.create('single', 3)
    print(store.update('single', 4, first['etag']))
    examples = json.loads(Path(__file__).with_name('examples.json').read_text())
    print(json.dumps(batch.run(store, examples['normal']), ensure_ascii=False, indent=2))
    print(list(store.db.execute('SELECT key,value,revision FROM records ORDER BY key')))
    store.close()
