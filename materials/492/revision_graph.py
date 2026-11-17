"""Public offline revision graph used by catalog inspection tools.

A revision is a string id mapped to an *ordered* list of parent ids.  The
represented graph is a DAG (at most two parents per revision).  ``applied_heads``
is a success frontier: every revision reachable from one of those heads is
known to be fully successful, and the frontier itself must be an antichain.

All construction and mutation validates first and only then replaces the
in-memory state, so an invalid definition leaves the existing graph untouched.
"""
from __future__ import annotations

import heapq


class InvalidRevisionGraph(ValueError):
    """Raised when a graph definition would be invalid."""


MAX_PARENTS = 2


def _check_id(value, what="revision id"):
    if not isinstance(value, str):
        raise InvalidRevisionGraph(f"{what} must be a string, got {type(value).__name__}")
    if not value:
        raise InvalidRevisionGraph(f"{what} must not be empty")


def _ancestors(parents, revision):
    """Inclusive ancestor closure of ``revision``."""
    seen = set()
    stack = [revision]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(parents[current])
    return seen


def _find_cycle(parents):
    """Return one cycle as a list (start == end) or ``None``."""
    color = {revision: 0 for revision in parents}  # 0 = new, 1 = active, 2 = done
    for start in parents:
        if color[start] != 0:
            continue
        color[start] = 1
        path = [start]
        stack = [(start, iter(parents[start]))]
        while stack:
            node, children = stack[-1]
            try:
                parent = next(children)
            except StopIteration:
                color[node] = 2
                stack.pop()
                path.pop()
                continue
            if color[parent] == 1:
                index = path.index(parent)
                return path[index:] + [parent]
            if color[parent] == 0:
                color[parent] = 1
                path.append(parent)
                stack.append((parent, iter(parents[parent])))
    return None


def _validate(parents):
    """Validate a complete parents mapping; raise InvalidRevisionGraph."""
    if not isinstance(parents, dict):
        raise InvalidRevisionGraph("parents must be a mapping of revision id to parent list")
    cleaned = {}
    for revision, values in parents.items():
        _check_id(revision)
        if isinstance(values, (str, bytes)):
            raise InvalidRevisionGraph(
                f"parents of {revision!r} must be a list of ids, not a string")
        try:
            values = list(values)
        except TypeError:
            raise InvalidRevisionGraph(f"parents of {revision!r} must be a list") from None
        if len(values) > MAX_PARENTS:
            raise InvalidRevisionGraph(
                f"revision {revision!r} has {len(values)} parents; at most {MAX_PARENTS}")
        for parent in values:
            _check_id(parent, "parent id")
        if len(set(values)) != len(values):
            raise InvalidRevisionGraph(f"revision {revision!r} repeats a parent")
        cleaned[revision] = values
    for revision, values in cleaned.items():
        for parent in values:
            if parent not in cleaned:
                raise InvalidRevisionGraph(
                    f"revision {revision!r} references unknown parent {parent!r}")
    cycle = _find_cycle(cleaned)
    if cycle is not None:
        raise InvalidRevisionGraph("cycle detected: " + " -> ".join(cycle))
    return cleaned


def _is_antichain(parents, revisions):
    """True when no revision in ``revisions`` is an ancestor of another."""
    members = set(revisions)
    for revision in members:
        ancestors = _ancestors(parents, revision) - {revision}
        if ancestors & members:
            return False
    return True


class RevisionGraph:
    def __init__(self, parents, applied_heads=()):
        cleaned = _validate(parents)
        heads = self._validate_heads(cleaned, applied_heads)
        self.parents = {key: list(value) for key, value in cleaned.items()}
        self.applied_heads = heads

    @staticmethod
    def _validate_heads(parents, applied_heads):
        if isinstance(applied_heads, (str, bytes)):
            raise InvalidRevisionGraph("applied_heads must be an iterable of ids")
        try:
            heads = list(applied_heads)
        except TypeError:
            raise InvalidRevisionGraph("applied_heads must be an iterable of ids") from None
        for head in heads:
            _check_id(head, "applied head")
            if head not in parents:
                raise InvalidRevisionGraph(f"applied head {head!r} is not a known revision")
        if len(set(heads)) != len(heads):
            raise InvalidRevisionGraph("applied_heads contains duplicates")
        if not _is_antichain(parents, heads):
            raise InvalidRevisionGraph(
                "applied_heads must be an antichain (no head may be an ancestor of another)")
        return set(heads)

    # -- validation helpers for external mutation -------------------------
    def _copy_parents(self):
        return {revision: list(values) for revision, values in self.parents.items()}

    def _commit_parents(self, parents, heads):
        self.parents = parents
        self.applied_heads = heads

    def add_revision(self, revision, parents, *, applied=False):
        """Add one revision atomically; an invalid addition changes nothing."""
        candidate = self._copy_parents()
        if revision in candidate:
            raise InvalidRevisionGraph(f"revision {revision!r} already exists")
        if isinstance(parents, (str, bytes)):
            raise InvalidRevisionGraph("parents must be a list of ids")
        try:
            candidate[revision] = list(parents)
        except TypeError:
            raise InvalidRevisionGraph(
                f"parents of {revision!r} must be a list of ids") from None
        cleaned = _validate(candidate)
        new_heads = set(self.applied_heads)
        if applied:
            missing = [p for p in cleaned[revision] if p not in self.successful()]
            if missing:
                raise InvalidRevisionGraph(
                    f'cannot mark {revision!r} applied: parent(s) not successful: '
                    f'{missing}')
            covered = _ancestors(cleaned, revision)
            new_heads = {head for head in new_heads if head not in covered}
            new_heads.add(revision)
            new_heads = self._validate_heads(cleaned, sorted(new_heads))
        self._commit_parents(cleaned, set(new_heads))

    def set_applied_heads(self, applied_heads):
        """Replace the success frontier atomically after validating it."""
        heads = self._validate_heads(self.parents, applied_heads)
        self.applied_heads = heads

    # -- queries ------------------------------------------------------------
    def heads(self):
        referenced = {p for values in self.parents.values() for p in values}
        return sorted(set(self.parents) - referenced)

    def successful(self):
        """All revisions known successful given the applied heads."""
        result = set()
        for head in self.applied_heads:
            result |= _ancestors(self.parents, head)
        return result

    def _topological(self, subset):
        """Parents-before-children order of ``subset`` (stable, id tie-break)."""
        indegree = {r: 0 for r in subset}
        children = {r: [] for r in subset}
        for revision in subset:
            for parent in self.parents[revision]:
                if parent in subset:
                    indegree[revision] += 1
                    children[parent].append(revision)
        ready = [r for r, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        order = []
        while ready:
            revision = heapq.heappop(ready)
            order.append(revision)
            for child in children[revision]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        if len(order) != len(subset):
            raise InvalidRevisionGraph("cycle detected while planning")
        return order

    def topological_order(self, revisions=None):
        """Parents-before-children order of ``revisions`` (default: whole graph).

        Ties are broken by revision id so the result is deterministic.
        """
        subset = set(self.parents) if revisions is None else set(revisions)
        unknown = subset - set(self.parents)
        if unknown:
            raise InvalidRevisionGraph(f"unknown revisions: {sorted(unknown)}")
        return self._topological(subset)

    def plan(self, targets):
        """Revisions in the targets' ancestor closure that are not yet successful.

        Each revision appears once, parents precede children, and revisions that
        share ancestors or merge are not duplicated.  Successful side branches
        already reachable from ``applied_heads`` are not returned but are still
        treated as available predecessors by ``record_success``.
        """
        if isinstance(targets, (str, bytes)):
            targets = [targets]
        targets = list(targets)
        for target in targets:
            _check_id(target, "target")
            if target not in self.parents:
                raise InvalidRevisionGraph(f"unknown target {target!r}")
        closure = set()
        for target in targets:
            closure |= _ancestors(self.parents, target)
        pending = closure - self.successful()
        return self._topological(pending)

    def record_success(self, revision):
        """Advance the success frontier for a *fully successful* revision.

        If ``revision`` is already covered by an applied head it is already
        known successful, so this is a no-op (the frontier stays an antichain).
        """
        if revision not in self.parents:
            raise InvalidRevisionGraph(f"unknown revision {revision!r}")
        if revision in self.successful() and revision not in self.applied_heads:
            return
        missing = [p for p in self.parents[revision] if p not in self.successful()]
        if missing:
            raise InvalidRevisionGraph(
                f"cannot record {revision!r}: parent(s) not successful: {missing}")
        # The new success covers its whole ancestor closure; drop every old head
        # inside that closure, but keep independent heads untouched.
        covered = _ancestors(self.parents, revision)
        new_heads = {head for head in self.applied_heads if head not in covered}
        new_heads.add(revision)
        self.applied_heads = self._validate_heads(self.parents, sorted(new_heads))
