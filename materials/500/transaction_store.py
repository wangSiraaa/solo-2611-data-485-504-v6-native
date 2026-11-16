"""Bounded relational store used by transactional data-maintenance tools."""
from copy import deepcopy


class IntegrityError(ValueError):
    pass


class TransactionStore:
    def __init__(self, catalog, relations):
        self.catalog = catalog
        self.committed = deepcopy(relations)
        self.current = None
        self.modes = {}
        self.marks = []

    def begin(self):
        if self.current is not None:
            raise ValueError('transaction already active')
        self.current = deepcopy(self.committed)
        self.modes = {name: ('deferred' if fk.deferrable and fk.initially_deferred
                             else 'immediate')
                      for name, fk in self.catalog.definitions.items()}
        self.marks = []

    def rows(self, *, committed=False):
        return deepcopy(self.committed if committed or self.current is None else self.current)

    def _check(self, names):
        bad = self.catalog.check_relations(self.current, names)
        if bad:
            raise IntegrityError(str(bad))

    def write(self, operation):
        if self.current is None:
            raise ValueError('no transaction')
        before = deepcopy(self.current)
        try:
            table, rowid = operation['table'], operation['rowid']
            if operation['kind'] == 'put':
                self.current[table][rowid] = deepcopy(operation['row'])
            elif operation['kind'] == 'delete':
                self.current[table].pop(rowid, None)
            else:
                raise ValueError('unknown write')
            action_errors = self.catalog.check_action(before, operation, self.current)
            if action_errors:
                raise IntegrityError(str(action_errors))
            self._check([name for name, mode in self.modes.items() if mode == 'immediate'])
        except Exception:
            self.current = before
            raise

    def set_constraints(self, names, mode):
        if mode not in ('immediate', 'deferred'):
            raise ValueError('unknown mode')
        if any(name not in self.catalog.definitions for name in names):
            raise ValueError('unknown constraint')
        if mode == 'deferred' and any(not self.catalog.definitions[name].deferrable for name in names):
            raise ValueError('not deferrable')
        for name in names:
            self.modes[name] = mode
        if mode == 'immediate':
            self._check(names)

    def savepoint(self, name):
        self.marks.append((name, deepcopy(self.current)))

    def rollback_to(self, name):
        index = max(i for i, mark in enumerate(self.marks) if mark[0] == name)
        self.current = deepcopy(self.marks[index][1])
        self.marks = self.marks[:index + 1]

    def release(self, name):
        index = max(i for i, mark in enumerate(self.marks) if mark[0] == name)
        self.marks = self.marks[:index]

    def commit(self):
        self._check(list(self.catalog.definitions))
        self.committed = deepcopy(self.current)
        self.current, self.modes, self.marks = None, {}, []

    def rollback(self):
        self.current, self.modes, self.marks = None, {}, []
