"""Public direct SQLite operation executor and graph-aware upgrade entry."""
class MigrationExecutor:
    def __init__(self,connection):
        self.connection=connection

    def execute(self, revision, operations):
        completed=[]
        try:
            self.connection.execute('BEGIN')
            for index,operation in enumerate(operations):
                if operation['kind']=='sql':self.connection.execute(operation['sql'])
                elif operation['kind']=='vacuum':
                    self.connection.commit()
                    self.connection.execute('VACUUM')
                    self.connection.execute('BEGIN')
                else:raise ValueError('unknown operation')
                completed.append(index)
            self.connection.commit()
            return dict(revision=revision,state='complete',completed=completed)
        except Exception as exc:
            self.connection.rollback()
            return dict(revision=revision,state='rolled_back',completed=[],error=str(exc))

    def upgrade(self,graph,revisions,targets):
        receipts=[]
        for revision in graph.plan(targets):
            receipt=self.execute(revision,revisions[revision])
            receipts.append(receipt)
            # The original runner advanced after every returned receipt.
            graph.record_success(revision)
        return receipts
