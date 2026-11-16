"""Callback-level transaction owner; returns one logical job result."""

class TransactionRunner:
    def __init__(self,session_factory):
        self.session_factory=session_factory;self.attempts=[]

    def run(self,callback,*,max_attempts=3):
        for attempt in range(1,max_attempts+1):
            session=self.session_factory();session.begin()
            try:
                value=callback(session)
                session.commit()
                self.attempts.append(dict(attempt=attempt,state='committed'))
                return value
            except Exception as exc:
                session.rollback()
                self.attempts.append(dict(attempt=attempt,state='rolled_back',error=type(exc).__name__))
                if attempt==max_attempts:raise
            finally:session.backend.close()
