"""Existing stream IPC public interface for non-worker file tools as well as jobs."""
import array
import json
import socket
import struct

class ChannelError(ValueError):
    pass

def send_bundle(sock, metadata, descriptors):
    payload = json.dumps(metadata, separators=(',', ':')).encode()
    frame = struct.pack('!I', len(payload)) + payload
    # The original local-only caller expected one sendmsg to send the whole frame.
    return sock.sendmsg([frame], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                  array.array('i', descriptors))])

def receive_bundle(sock, *, max_descriptors=3):
    data, ancillary, flags, _ = sock.recvmsg(65536, socket.CMSG_SPACE(max_descriptors * array.array('i').itemsize))
    descriptors = []
    for level, kind, raw in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            values = array.array('i'); values.frombytes(raw[:len(raw) - len(raw) % values.itemsize])
            descriptors.extend(values)
    if len(data) < 4:
        raise ChannelError('partial header')
    length = struct.unpack('!I', data[:4])[0]
    if len(data) != length + 4:
        raise ChannelError('partial or concatenated message')
    metadata = json.loads(data[4:])
    if len(descriptors) != metadata['fd_count']:
        raise ChannelError('descriptor count mismatch')
    return metadata, descriptors
