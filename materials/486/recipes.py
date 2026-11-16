"""Bounded real table recipes; data maintenance uses execute without policy planning."""
from policy import evaluate


def execute(graph, root, bindings):
    node = graph[root]
    kind = node['op']
    if kind == 'source':
        rows = [dict(row, _sources=[root]) for row in node['rows']]
        return rows, node['cost']
    if kind == 'choice':
        return execute(graph, bindings[root], bindings)
    parts = [execute(graph, parent, bindings) for parent in node['inputs']]
    if kind == 'concat':
        rows = [row for data, _ in parts for row in data]
    elif kind == 'project':
        rows = [{column: row[column] for column in node['columns']} |
                {'_sources': list(row['_sources'])} for row in parts[0][0]]
    elif kind == 'join':
        rows = []
        for left in parts[0][0]:
            for right in parts[1][0]:
                if left[node['key']] == right[node['key']]:
                    rows.append({**left, **right,
                        '_sources': sorted(set(left['_sources']) | set(right['_sources']))})
    else:
        raise ValueError(kind)
    return rows, node['cost'] + sum(cost for _, cost in parts)


def cheapest_bindings(graph):
    bindings = {}
    for node_id, node in graph.items():
        if node['op'] == 'choice':
            bindings[node_id] = min(node['alternatives'], key=lambda name: graph[name]['cost'])
    return bindings


def plan_and_deliver(graph, root, purpose):
    bindings = cheapest_bindings(graph)
    decision = evaluate(graph, root, bindings, purpose)
    if not decision['allowed']:
        return {'status': 'no-plan', 'bindings': bindings, 'policy': decision}
    rows, cost = execute(graph, root, bindings)
    return {'status': 'ok', 'bindings': bindings, 'cost': cost,
            'rows': rows, 'policy': decision}

