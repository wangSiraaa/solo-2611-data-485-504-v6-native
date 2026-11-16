"""Relation integrity checks used directly by an offline import preflight."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ForeignKey:
    name: str
    child: str
    parent: str
    child_columns: tuple
    parent_columns: tuple
    on_delete: str = 'NO ACTION'
    deferrable: bool = True
    initially_deferred: bool = True


class ConstraintCatalog:
    def __init__(self, definitions):
        self.definitions = {fk.name: fk for fk in definitions}

    def check_relations(self, relations, names=None):
        violations = []
        for name in self.definitions if names is None else names:
            fk = self.definitions[name]
            parent_keys = {tuple(row[c] for c in fk.parent_columns)
                           for row in relations[fk.parent].values()}
            for rowid, row in relations[fk.child].items():
                key = tuple(row[c] for c in fk.child_columns)
                if all(value is None for value in key):
                    continue
                if key not in parent_keys:
                    violations.append({'constraint': name, 'table': fk.child,
                                       'rowid': rowid, 'key': list(key)})
        return violations

    def check_action(self, before, operation, after):
        # The initial importer only checked complete resulting relations.
        return []
