"""End-to-end entry: quality batch evidence feeding generic fault observations.

The two services stay independent: ``QualityRuns`` never talks to
``IncidentService`` and external incidents keep using ``IncidentService``
alone.  ``QualityIncidentBridge`` is the one real integration point.  It
ingests the batch, then forwards that run's derived quality observation to the
incident service using the *same check / run identity and sequence*:

    key=check, observation_id=run_id, sequence=sequence, value=observation

Because the incident service performs its own same-identity/ same-content
de-duplication, re-delivering one quality run is idempotent and cannot open an
extra episode.  Operator activity (ack/silence/advance) never modifies the
quality reports, and the bridge never re-sends observations on those actions.
"""
from copy import deepcopy


class QualityIncidentBridge:
    def __init__(self, quality, incidents):
        self.quality = quality
        self.incidents = incidents

    def process(self, check, run_id, sequence, rows, expected_ids, complete=True):
        """Ingest one batch and forward its observation; return both records."""
        result = self.quality.ingest(
            check, run_id, sequence, rows, expected_ids, complete)
        incident = self.incidents.observe(
            check, run_id, sequence, result['observation'])
        return {'quality': result, 'incident': incident}

    def view(self, check):
        """Report quality evidence plus the key's incident/notification state."""
        report = self.quality.report(check)
        return {
            'quality': report,
            'incident': self.incidents.inspect(check),
            'episodes': self.incidents.episodes(check),
            'notifications': self.incidents.notification_records(check),
        }

    def ack(self, check):
        return self.incidents.ack(check)

    def silence(self, check, until):
        return self.incidents.silence(check, until)

    def unsilence(self, check):
        return self.incidents.unsilence(check)

    def advance(self, tick):
        return self.incidents.advance(tick)
