Code is a JSON instruction array, immutable for a call, with an explicit schema
and result {base,nullable}. Types are int64, bool, text (<=128 codepoints), with
typed NULL. Python bool is not int. PUSH includes its base type; None is a NULL
of that type. LOAD references a schema field; legal rows contain exactly those
fields and conform to their types. No arbitrary Python/eval or function imports.

Static admission never executes real or generated rows. It checks finite CFG
reachability from pc0, with both BRANCH edges potentially reachable regardless
of constant values, and a worklist/fixed point at joins/backedges. Each reached pc
must have one stack height and compatible base types from every incoming edge;
nullable is joined by OR. ADD/DIV need two int operands; EQ needs equal bases;
IS_NULL accepts one value and returns nonnull bool; BRANCH consumes bool/nullable
bool and has target/fall-through successors; JUMP has one successor. DUP/POP need
available values. RETURN must have exactly one value compatible with the declared
result, including nullability. Reachable fall-off or out-of-range target rejects.
All opcode and operand syntax is checked, even in unreachable instructions.
Unreachable stack errors need not reject. Growing-stack or base-incompatible
backedges reject; a type-safe infinite loop is allowed and handled by runtime fuel.
The public offline registry saves an accepted code hash, schema/result contract
and maximum reachable stack; no artifact is published for rejection. This serves
non-executing plugin catalogs. It does not claim numeric safety or termination.

The independent VM also runs externally supplied already-verified legal code,
without requiring a registration artifact produced by this registry. It executes
only the chosen branch; BRANCH NULL follows fall-through. ADD/DIV/EQ propagate
NULL before numeric checks (including NULL divided by0 => NULL). Integer division
is floor division. ADD/DIV int64 overflow and zero divisor are per-row errors.
IS_NULL is never NULL. Each executed instruction costs one unit of fuel; every
row starts with the full provided budget. A fault/overflow/fuel exhaustion ends
only that row and preserves other row results/identity. Return a value or a
specific error per row; skipped paths produce neither numeric errors nor fuel use.
The VM may revalidate, share helpers, or use a mature backend; no implementation
layout, JIT, vectorization or proof of general program termination is required.

Limits: 32 instructions, maximum accepted stack16, 50 rows, fuel1..100 per row.
Static catalog calls have no dataset. Execution and admission are public contracts
with separate consumers; whether they form real independent production duties
must be assessed from these behaviors, not simply from source-file count.
