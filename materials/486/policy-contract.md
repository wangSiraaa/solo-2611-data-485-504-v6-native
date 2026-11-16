These are fictional, explicit machine-enforced data-use terms, not an
interpretation of real licenses or law. Fields are not inferred from names.

Each source contributes a group with its singleton source identity, allowed
purposes, mandatory attribution, share class (permissive/reciprocal/private),
and no_derive boolean. A source used anywhere must grant the requested purpose;
all its attribution is retained, even if projection removes all value columns.

concat creates a collection: retain its inputs' groups independently. It is not
a derivation, so reciprocal and private groups may coexist in a collection;
no_derive sources can also be collected. Duplicate occurrences preserve data
multiplicity, but policy source identities and attribution are deduplicated.

project is a derivation of each retained input group. Any no_derive source in
an input group forbids project, including a projection which keeps every column.
Otherwise groups remain separate with all original policies and provenance.

join derives one group from every group of both inputs. Any no_derive input
forbids it. A derived group containing both reciprocal and private sources is
forbidden; permissive can combine with either, and reciprocal+reciprocal is
allowed. This rule concerns the transformation graph, even if a join has zero
matching data rows. No source is dropped from policy attribution for an empty
result. Equal sources do not conflict with themselves. Invalid derivation at
any reachable node makes the selected plan invalid. Report conflicting node
identities and source identities; exact message prose is not graded.

The graph is a finite DAG. choice selects exactly one of its named alternatives;
one choice identity has one binding across all its uses. Unreachable branches
do not execute, contribute policies, or incur cost. Costs are nonnegative
integers, charged once per reachable identity (source and processing node);
choice has cost 0. Data multiplicity is unaffected by memoizing a shared node.
project has one input and explicit existing columns; concat inputs have matching
schemas; join has two inputs and one common key, other column names are disjoint.
Rows contain integers or strings, no NULL, and join uses ordinary exact equality
and preserves duplicate matches. _sources is reserved provenance metadata.

The independent evaluate entry must handle fully bound reachable recipes for
procurement clients without reading rows. The independent execute entry builds
fully bound recipes without applying use policy, for maintenance clients. The
delivery entry chooses a legal complete binding of minimum total reachable cost,
actually builds rows, and returns binding, cost, source identities, attribution,
and a derivation policy explanation. No plan returns no-plan with an explanation.
Ties use lexicographically smallest sorted (choice identity, selected identity)
list for reachable choices; choices only in unused branches are not bindings.

Up to 12 source identities, 20 total nodes, 8 choice identities, 2 alternatives
per choice, 50 rows/source and at most 2000 output rows. Single process, local
data, no persistent catalog. Full enumeration, full loading, mature constraint
or graph libraries and merged implementation are all permitted. No asymptotic
optimality or legal-policy inference is required.
