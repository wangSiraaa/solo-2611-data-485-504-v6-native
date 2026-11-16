"""Dataset-use contract service; procurement also calls this without reading rows."""
def evaluate(graph, root, bindings, purpose):
    sources = set()

    def visit(node_id):
        node = graph[node_id]
        kind = node['op']
        if kind == 'source':
            sources.add(node_id)
        elif kind == 'choice':
            visit(bindings[node_id])
        else:
            for parent in node['inputs']:
                visit(parent)

    visit(root)
    allowed = all(purpose in graph[source]['policy']['purposes'] for source in sources)
    return {'allowed': allowed,
            'attribution': sorted({graph[s]['policy']['attribution'] for s in sources}),
            'sources': sorted(sources), 'conflicts': []}

