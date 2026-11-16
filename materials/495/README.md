# Bounded arrays and relational consumers

Python 3.11 suffices. `array_values.py` is a mutable array SDK with standalone
import clients. `array_query.py` consumes already constructed values and can be
called without the SDK's slice/assignment path. The ordinary lower-bound-one
case works. The prompt defines rank, bounds, slice reset, atomic assignment,
three-valued quantifiers and padded parallel unnesting.

`examples.json` includes lower-bound zero specifically so Python negative-index
wrapping cannot silently make a lower-bound defect look correct. Mature array
or SQL facilities are allowed; the starter's Python truth values are not an
oracle for SQL UNKNOWN.
