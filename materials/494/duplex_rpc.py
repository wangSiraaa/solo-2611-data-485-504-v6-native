"""Public two-way JSON-RPC endpoint, also used outside catalog jobs."""
import json

class RPCError(RuntimeError):
    pass

class Endpoint:
    def __init__(self,sock,handlers=None):
        self.sock=sock;self.stream=sock.makefile('rwb',buffering=0)
        self.handlers=dict(handlers or {});self.serial=0

    def _send(self,message):
        self.stream.write(json.dumps(message,separators=(',',':')).encode()+b'\n')

    def _receive(self):
        line=self.stream.readline()
        if not line:raise RPCError('peer closed')
        return json.loads(line)

    def call(self,method,params):
        self.serial+=1;identity=self.serial
        self._send(dict(jsonrpc='2.0',id=identity,method=method,params=params))
        message=self._receive()
        if 'method' in message:raise RPCError('reverse request arrived while waiting')
        if message.get('id')!=identity:raise RPCError('response belongs to another call')
        if 'error' in message:raise RPCError(message['error']['message'])
        return message['result']

    def serve_one(self):
        message=self._receive()
        if 'method' not in message:raise RPCError('expected request')
        try:
            value=self.handlers[message['method']](**message.get('params',{}))
            response=dict(jsonrpc='2.0',id=message['id'],result=value)
        except Exception as exc:
            response=dict(jsonrpc='2.0',id=message['id'],error=dict(code=-32000,message=str(exc)))
        self._send(response)

    def close(self):
        self.stream.close();self.sock.close()
