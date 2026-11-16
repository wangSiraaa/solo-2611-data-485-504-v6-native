"""External CSV table transaction participant.

Two clients share one CSV file through this object:

* non-SQL maintenance tools drive the begin/put/remove/savepoint/.../commit
  API directly;
* the APSW virtual table adapter in :mod:`virtual` maps SQLite transaction
  and row-update callbacks onto the same API.

All mutations operate on an in-memory *private snapshot* taken by
:meth:`begin`.  The CSV on disk is only rewritten inside :meth:`commit`,
after :meth:`prepare` has accepted the transaction, so rollback of a
statement, a savepoint or the whole transaction can never leak private
rows into the file.
"""
import csv
import os
from pathlib import Path


class TableTransactions:
    def __init__(self, path, total_limit=100):
        self.path = Path(path)
        self.total_limit = total_limit
        self.active = False
        self.private = None
        # Live savepoints as an ordered stack of (number, state snapshot).
        # Each snapshot is an independent dict, so later mutations can never
        # alias a previously saved state.
        self.marks = []
        self.prepared = False
        self.events = []

    # ------------------------------------------------------------------ reads
    def disk_rows(self):
        """Rows currently committed in the CSV file."""
        if not self.path.exists():
            return {}
        with self.path.open(newline='') as stream:
            return {int(r['id']): int(r['value']) for r in csv.DictReader(stream)}

    def rows(self):
        """Private rows inside a transaction, committed file rows otherwise."""
        source = self.private if self.active else self.disk_rows()
        return dict(source)

    # ------------------------------------------------------------- transaction
    def begin(self):
        if self.active:
            raise ValueError('transaction already active')
        self.active = True
        self.private = self.disk_rows()
        self.marks = []
        self.prepared = False
        self.events.append(['begin'])

    def put(self, key, value):
        if not self.active or type(key) is not int or type(value) is not int or value < 0:
            raise ValueError('invalid mutation')
        self.private[key] = value
        self.prepared = False
        self.events.append(['put', key, value])

    def remove(self, key):
        if not self.active:
            raise ValueError('not active')
        self.private.pop(key, None)
        self.prepared = False
        self.events.append(['remove', key])

    # --------------------------------------------------------------- savepoints
    def savepoint(self, number):
        """Snapshot the current private state under ``number``."""
        if not self.active:
            raise ValueError('not active')
        self.marks.append((number, dict(self.private)))
        self.events.append(['savepoint', number])

    def rollback_to(self, number):
        """Restore the state saved under ``number``; drop younger marks."""
        for index in range(len(self.marks) - 1, -1, -1):
            if self.marks[index][0] == number:
                # Independent dict: restoring cannot mutate the saved mark.
                self.private = dict(self.marks[index][1])
                del self.marks[index + 1:]
                self.prepared = False
                self.events.append(['rollback_to', number])
                return
        raise ValueError('unknown savepoint')

    def release(self, number):
        """Drop the mark ``number`` and every younger mark; keep outer data."""
        for index in range(len(self.marks)):
            if self.marks[index][0] == number:
                del self.marks[index:]
                self.events.append(['release', number])
                return
        raise ValueError('unknown savepoint')

    # ------------------------------------------------------- prepare / publish
    def prepare(self):
        """Validate the transaction.  Does not touch the CSV file."""
        if not self.active:
            raise ValueError('not active')
        if sum(self.private.values()) > self.total_limit:
            raise ValueError('external table total exceeds limit')
        self.prepared = True
        self.events.append(['prepare'])

    def publish_preview(self):
        """Write the private snapshot to disk via a temp file + os.replace."""
        temp = self.path.with_suffix('.pending')
        with temp.open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['id', 'value'])
            writer.writerows(sorted(self.private.items()))
        os.replace(temp, self.path)
        self.events.append(['file_replaced'])

    def commit(self):
        if not self.active:
            raise ValueError('not active')
        if not self.prepared:
            # Direct callers call prepare() themselves; stay safe if they do.
            self.prepare()
        self.publish_preview()
        self.active = False
        self.private = None
        self.marks = []
        self.prepared = False
        self.events.append(['commit'])

    def rollback(self):
        self.active = False
        self.private = None
        self.marks = []
        self.prepared = False
        self.events.append(['rollback'])
