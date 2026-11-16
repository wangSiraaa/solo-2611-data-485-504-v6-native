"""Offline code admission; registry clients have no data rows and never run the VM."""
import hashlib
import json
from pathlib import Path


class VerificationError(ValueError):
    pass


def validate(program, schema):
    code = program['code']
    stack = []
    pc = 0
    visited = set()
    maximum = 0
    while pc not in visited:
        if not 0 <= pc < len(code):
            raise VerificationError('control falls outside code')
        visited.add(pc)
        instruction = code[pc]
        op = instruction['op']
        if op == 'LOAD':
            stack.append(schema[instruction['field']])
        elif op == 'PUSH':
            stack.append({'base': instruction['type'], 'nullable': instruction['value'] is None})
        elif op == 'DUP':
            stack.append(stack[-1])
        elif op == 'POP':
            stack.pop()
        elif op in ('ADD', 'DIV', 'EQ'):
            right, left = stack.pop(), stack.pop()
            if left['base'] != right['base'] or (op != 'EQ' and left['base'] != 'int'):
                raise VerificationError('operand type mismatch')
            stack.append({'base': 'bool' if op == 'EQ' else 'int',
                          'nullable': left['nullable'] or right['nullable']})
        elif op == 'IS_NULL':
            stack.pop()
            stack.append({'base': 'bool', 'nullable': False})
        elif op == 'JUMP':
            pc = instruction['target']
            continue
        elif op == 'BRANCH':
            if stack.pop()['base'] != 'bool':
                raise VerificationError('branch needs bool')
            # Linear legacy admission follows only the fall-through branch.
        elif op == 'RETURN':
            if len(stack) != 1 or stack[0]['base'] != program['result']['base']:
                raise VerificationError('return stack/type mismatch')
            return {'accepted': True, 'result': stack[0], 'maximum_stack': maximum}
        else:
            raise VerificationError('unknown opcode')
        maximum = max(maximum, len(stack))
        pc += 1
    return {'accepted': True, 'result': program['result'], 'maximum_stack': maximum}


class ProgramRegistry:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)

    def admit(self, name, program, schema):
        analysis = validate(program, schema)
        body = json.dumps(program, sort_keys=True, separators=(',', ':')).encode()
        record = {'program_sha256': hashlib.sha256(body).hexdigest(),
                  'schema': schema, 'analysis': analysis, 'program': program}
        (self.root / (name + '.json')).write_text(json.dumps(record))
        return record

