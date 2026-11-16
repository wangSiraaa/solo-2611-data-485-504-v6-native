"""Concrete batch VM; external trusted producers also provide already-verified code."""
def run(program, rows, fuel=64):
    results = []
    remaining = fuel
    try:
        for row in rows:
            stack = []
            pc = 0
            while True:
                if remaining == 0:
                    raise ValueError('fuel exhausted')
                remaining -= 1
                instruction = program['code'][pc]
                op = instruction['op']
                if op == 'LOAD':
                    stack.append(row[instruction['field']])
                elif op == 'PUSH':
                    stack.append(instruction['value'])
                elif op == 'DUP':
                    stack.append(stack[-1])
                elif op == 'POP':
                    stack.pop()
                elif op == 'IS_NULL':
                    stack.append(stack.pop() is None)
                elif op in ('ADD', 'DIV', 'EQ'):
                    right, left = stack.pop(), stack.pop()
                    if op == 'ADD':
                        value = (left or 0) + (right or 0)
                    elif op == 'DIV':
                        value = (left or 0) // (right or 0)
                    else:
                        value = left == right
                    stack.append(value)
                elif op == 'BRANCH':
                    if stack.pop() is True:
                        pc = instruction['target']
                        continue
                elif op == 'JUMP':
                    pc = instruction['target']
                    continue
                elif op == 'RETURN':
                    results.append({'status': 'ok', 'value': stack.pop()})
                    break
                else:
                    raise ValueError('unknown opcode')
                pc += 1
    except Exception as error:
        return {'status': 'failed', 'results': results, 'error': type(error).__name__ + ': ' + str(error)}
    return {'status': 'complete', 'results': results}

