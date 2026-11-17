"""Real JSON-file transport for trusted transfer messages.

Messages are plain JSON documents exported by a sender and read by a receiver.
This is a manual file hand-off; there is no network, signature or sender
authentication.  Validation of fields, destination and positive units is
still mandatory and is enforced by ``Rights.receive`` as well as here.
"""


def export_message(message, path):
    """Write the exact canonical message to ``path`` as JSON."""
    try:  # works both as a flat module and as part of a namespace package
        from rights import canonical
    except ImportError:  # pragma: no cover - package-style import
        from .rights import canonical

    if not isinstance(message, dict):
        raise ValueError('invalid message')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(canonical(message))
        handle.write('\n')
    return path


def read_message(path):
    """Read a JSON transfer message from a file; object key order is free."""
    import json

    with open(path, 'r', encoding='utf-8') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError('invalid message document')
    return value
