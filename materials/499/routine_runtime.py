"""A registered data-routine runner with independent direct-query callers."""


class RoutineRuntime:
    def __init__(self, db, catalog, definitions, session_user):
        self.db = db
        self.catalog = catalog
        self.definitions = definitions
        self.session_user = session_user
        self.effective_principal = session_user

    def query(self, object_name):
        if object_name != 'facts':
            raise ValueError('unregistered object')
        if not self.catalog.can_select(self.effective_principal, object_name):
            raise PermissionError(self.effective_principal + ' cannot SELECT ' + object_name)
        return self.db.execute('SELECT id, value FROM facts ORDER BY id').fetchall()

    def run(self, routine_name):
        definition = self.definitions[routine_name]
        previous = self.effective_principal
        self.effective_principal = (definition['owner'] if definition['security'] == 'definer'
                                    else self.session_user)
        output = []
        for action in definition['actions']:
            if action['kind'] == 'select':
                output.extend(self.query(action['object']))
            elif action['kind'] == 'call':
                output.extend(self.run(action['routine']))
            elif action['kind'] == 'raise':
                raise RuntimeError(action['message'])
            else:
                raise ValueError('unsupported action')
        self.effective_principal = previous
        return output
