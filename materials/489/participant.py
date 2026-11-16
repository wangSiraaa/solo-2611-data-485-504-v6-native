"""Small external CSV table transaction participant used by multiple clients."""
import copy
import csv
from pathlib import Path
import os


class TableTransactions:
    def __init__(self, path, total_limit=100):
        self.path = Path(path)
        self.total_limit = total_limit
        self.active = False
        self.private = None
        self.marks = {}
        self.events = []

    def disk_rows(self):
        with self.path.open(newline='') as stream:
            return {int(r['id']): int(r['value']) for r in csv.DictReader(stream)}

    def rows(self):
        return dict(self.private if self.active else self.disk_rows())

    def begin(self):
        if self.active:
            raise ValueError('transaction already active')
        self.active = True
        self.private = self.disk_rows()
        self.marks = {}
        self.events.append(['begin'])

    def put(self, key, value):
        if not self.active or type(key) is not int or type(value) is not int or value < 0:
            raise ValueError('invalid mutation')
        self.private[key] = value
        self.events.append(['put', key, value])

    def remove(self, key):
        if not self.active:
            raise ValueError('not active')
        self.private.pop(key, None)
        self.events.append(['remove', key])

    def savepoint(self, number):
        if not self.active:
            raise ValueError('not active')
        self.marks[number] = self.private
        self.events.append(['savepoint', number])

    def rollback_to(self, number):
        if number not in self.marks:
            raise ValueError('unknown savepoint')
        self.private = self.marks[number]
        self.marks = {k: v for k, v in self.marks.items() if k <= number}
        self.events.append(['rollback_to', number])

    def release(self, number):
        self.marks = {k: v for k, v in self.marks.items() if k < number}
        self.events.append(['release', number])

    def prepare(self):
        if not self.active or sum(self.private.values()) > self.total_limit:
            raise ValueError('external table total exceeds limit')
        self.events.append(['prepare'])

    def publish_preview(self):
        temp = self.path.with_suffix('.pending')
        with temp.open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['id', 'value'])
            writer.writerows(sorted(self.private.items()))
        os.replace(temp, self.path)
        self.events.append(['file_replaced'])

    def commit(self):
        self.publish_preview()
        self.active, self.private, self.marks = False, None, {}
        self.events.append(['commit'])

    def rollback(self):
        self.active, self.private, self.marks = False, None, {}
        self.events.append(['rollback'])
