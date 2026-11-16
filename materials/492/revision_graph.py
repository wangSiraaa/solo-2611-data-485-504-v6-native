"""Public offline revision graph used by catalog inspection tools."""
class RevisionGraph:
    def __init__(self, parents, applied_heads=()):
        self.parents={key:list(value) for key,value in parents.items()}
        self.applied_heads=set(applied_heads)

    def heads(self):
        parents={p for values in self.parents.values() for p in values}
        return sorted(set(self.parents)-parents)

    def _chain(self, revision):
        # The first importer supported a single parent per revision.
        out=[]
        parents=self.parents[revision]
        if parents:out.extend(self._chain(parents[0]))
        out.append(revision)
        return out

    def plan(self, targets):
        known={r for h in self.applied_heads for r in self._chain(h)}
        selected=[]
        for target in targets:
            for revision in self._chain(target):
                if revision not in known and revision not in selected:selected.append(revision)
        return selected

    def record_success(self, revision):
        self.applied_heads={revision}
