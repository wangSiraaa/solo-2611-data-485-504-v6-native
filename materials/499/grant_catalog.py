"""Small warehouse grant directory, used without the routine runtime too."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Grant:
    grantor: str
    grantee: str
    object_name: str
    grant_option: bool


class GrantError(ValueError):
    pass


class GrantCatalog:
    def __init__(self, root='root'):
        self.root = root
        self.grants = []

    def can_select(self, principal, object_name, *, grant_option=False):
        return principal == self.root or any(
            row.grantee == principal and row.object_name == object_name
            and (row.grant_option or not grant_option) for row in self.grants)

    def grant(self, grantor, grantee, object_name, *, grant_option=False):
        if not self.can_select(grantor, object_name, grant_option=True):
            raise GrantError('grantor lacks grant option')
        row = Grant(grantor, grantee, object_name, grant_option)
        if row not in self.grants:
            self.grants.append(row)

    def revoke(self, grantor, grantee, object_name, *, behavior='CASCADE'):
        removed = [row for row in self.grants if row.grantor == grantor
                   and row.grantee == grantee and row.object_name == object_name]
        if not removed:
            raise GrantError('no such grant')
        downstream = [row for row in self.grants if row.grantor == grantee
                      and row.object_name == object_name]
        if behavior == 'RESTRICT' and downstream:
            raise GrantError('dependent grant exists')
        if behavior not in ('CASCADE', 'RESTRICT'):
            raise GrantError('unknown revoke behavior')
        self.grants = [row for row in self.grants if row not in removed]
        pending = [grantee]
        while pending:
            principal = pending.pop()
            affected = [row for row in self.grants if row.grantor == principal
                        and row.object_name == object_name]
            self.grants = [row for row in self.grants if row not in affected]
            pending.extend(row.grantee for row in affected)

    def inspect_grants(self):
        return [vars(row) for row in self.grants]
