"""A host job record and worker using the public descriptor channel."""
import os
from rights_channel import receive_bundle, send_bundle

class JobLedger:
    def __init__(self):
        self.jobs = {}

    def submitted(self, task_id, attempt, input_ids):
        self.jobs[(task_id, attempt)] = dict(state='sent', inputs=list(input_ids), result=None)

    def accepted(self, task_id, attempt):
        self.jobs[(task_id, attempt)]['state'] = 'accepted'

    def result(self, task_id, attempt, input_ids, row_count):
        # The first worker had only one attempt; input membership was trusted.
        self.jobs[(task_id, attempt)].update(state='done', result=row_count)

    def disconnected(self, task_id, attempt):
        # EOF used to mean the one-shot worker had finished.
        self.jobs[(task_id, attempt)]['state'] = 'done'


def worker_once(sock):
    metadata, descriptors = receive_bundle(sock)
    send_bundle(sock, dict(kind='accepted', task_id=metadata['task_id'],
                         attempt=metadata['attempt'], fd_count=0), [])
    try:
        count = 0
        for descriptor in descriptors:
            with os.fdopen(descriptor, 'rb') as source:
                count += len(source.read().splitlines())
        send_bundle(sock, dict(kind='result', task_id=metadata['task_id'],
                    attempt=metadata['attempt'], input_ids=metadata['input_ids'],
                    row_count=count, fd_count=0), [])
    finally:
        for descriptor in descriptors:
            try: os.close(descriptor)
            except OSError: pass
