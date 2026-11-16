"""Persistent delivery claims for preexisting outbox events; no fact mutation."""
from warehouse import connect


class Relay:
    def __init__(self, path):
        self.db = connect(path)

    def claim(self, owner, now, duration=5):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute("SELECT * FROM outbox WHERE status='pending' OR "
                                  "(status='inflight' AND lease_until<=?) ORDER BY event_id LIMIT 1",
                                  (now,)).fetchone()
            if row is None:
                self.db.commit()
                return None
            ticket = dict(row)
            ticket.update(owner=owner, generation=row['generation'] + 1,
                          lease_until=now + duration)
            self.db.execute("UPDATE outbox SET status='inflight',owner=?,generation=?,lease_until=? "
                            'WHERE event_id=?',
                            (owner, ticket['generation'], ticket['lease_until'], row['event_id']))
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?)',
                            (row['event_id'], ticket['generation'], owner, 'claimed'))
            self.db.commit()
            return ticket
        except Exception:
            self.db.rollback()
            raise

    def confirm(self, ticket, now):
        self.db.execute("UPDATE outbox SET status='delivered',owner=NULL,lease_until=NULL WHERE event_id=?",
                        (ticket['event_id'],))
        self.db.execute("UPDATE attempts SET state='confirmed' WHERE event_id=? AND generation=?",
                        (ticket['event_id'], ticket['generation']))
        return True

    def fail(self, ticket, now, *, unknown=False):
        self.db.execute("UPDATE outbox SET status='pending',owner=NULL,lease_until=NULL WHERE event_id=?",
                        (ticket['event_id'],))
        self.db.execute('UPDATE attempts SET state=? WHERE event_id=? AND generation=?',
                        ('unknown' if unknown else 'failed', ticket['event_id'], ticket['generation']))
        return True

    def renew(self, ticket, now, duration=5):
        self.db.execute('UPDATE outbox SET lease_until=? WHERE event_id=?',
                        (now + duration, ticket['event_id']))
        return True

    def deliver_one(self, owner, clock, transport):
        ticket = self.claim(owner, clock(), 5)
        if ticket is None:
            return None
        try:
            transport.send(ticket['event_id'], ticket['payload'])
        except TimeoutError:
            self.fail(ticket, clock(), unknown=True)
        else:
            self.confirm(ticket, clock())
        return ticket
