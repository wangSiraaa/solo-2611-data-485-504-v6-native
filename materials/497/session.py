"""Public SQL client used directly as well as by a transaction runner."""
from backend import DriverFault

class SessionError(RuntimeError):
    pass

class Session:
    def __init__(self,backend):
        self.backend=backend;self.state='idle'

    def begin(self):
        self.backend.begin();self.state='active'

    def execute(self,sql,params=()):
        # An older statement adapter retries inside a transaction it does not own.
        for attempt in range(2):
            try:return self.backend.execute(sql,params)
            except DriverFault as exc:
                if attempt==0 and exc.sqlstate=='40001':continue
                raise SessionError(str(exc)) from None

    def commit(self):
        try:
            self.backend.commit();self.state='idle'
        except DriverFault as exc:
            raise SessionError(str(exc)) from None

    def rollback(self):
        self.backend.rollback();self.state='idle'
