# 100,000-row bounded preparation implementation plan

## Status and authority

**Status:** In progress on 2026-08-06. Increments 1-3 are implemented for
direct datasets. Increment 4 remains open for related/derived BOM behavior,
so P3 is not yet complete. The supported product boundary is now 50,000 rows
for bounded direct projects and 25,000 rows for derived/materialized projects.
This is the implementation plan for P3 of the
[100,000-row performance refactor plan](100k-performance-refactor-plan.md).
It is a cross-cutting performance work package, not the product's numbered
Slice 6.

P3 is complete only when the Products and BOM fixtures prepare source evidence
without retaining complete physical, prepared, canonical, and transformation-
impact object graphs at the same time. A 100,000-row product promise remains
blocked until P5 closes every release gate.

## Outcome

Replace the current materializing path:

```text
complete SourceTable collection
-> complete staged SourceTable collection
-> complete PreparedBundle
-> complete CanonicalStagingRun
-> complete transformation-impact list
```

with this bounded path:

```text
verify immutable source bytes
-> read selected rows in fixed-size batches
-> apply compiled Python row transformations
-> append provisional rows, lineage, keys, and impacts to a preparation session
-> finalize cross-row rules in deterministic order
-> stream final rows through the canonical hasher and control-total accumulator
-> atomically publish the validated canonical run
```

The slice is intended to lower true peak working set. It is not justified by a
comparison between the historical Lenovo/Windows and MacBook Air/macOS runs.
Performance claims require controlled runs on the same machine, fixture,
revision, Python environment, batch size, and benchmark command.

## Measured problem

The corrected 2026-08-06 MacBook Air M5 diagnostics continuously sampled
working set and observed these complete-workflow peaks:

| Fixture | Physical rows | Complete time | Observed peak | Ending RSS | Effects |
| --- | ---: | ---: | ---: | ---: | ---: |
| Products | 100,000 | 26.929 s | 1,605.0 MiB | 829.0 MiB | 100,000 |
| BOM | 100,000 | 61.961 s | 1,685.6 MiB | 789.6 MiB | 300,000 |

Both pass the 120-second time gate and fail the 900-MiB peak-memory gate. The
peak occurs during source evaluation, when several row-proportional Python
representations coexist. Releasing the prepared bundle before quality lowered
later residency but could not address that peak.

## 2026-08-06 implementation evidence

Controlled fresh-process probes on the same MacBook Air M5, repository,
fixture generator, Python environment, 5,000-row source/session batch, and
96-MiB session DuckDB limit produced:

| Fixture and scope | Rows | Time | Peak working set | Ending RSS | Database |
| --- | ---: | ---: | ---: | ---: | ---: |
| Direct Products, source through session finalization | 100,000 | 44.384 s | 750.6 MiB | 602.2 MiB | 296.5 MiB |
| Direct BOM, source through session finalization | 100,000 | 50.555 s | 744.0 MiB | 737.2 MiB | 314.5 MiB |
| Products, complete Stage E-G workflow | 100,000 | 88.661 s | 1,608.6 MiB | 664.2 MiB | 374.3 MiB |

All results in the table use the final 96-MiB session setting. The Products
source duration inside the complete run was 43.695 seconds, consistent with
the isolated result.

The direct source phase now passes both phase gates with meaningful margin.
Exact small-fixture canonical bytes and hashes match the materializing oracle;
CSV/XLSX batch sizes `1`, `17`, and the production default preserve reader and
transformer behavior; duplicate identities spanning batches pass through the
bounded exception finalizer; and failure cleanup retains only a safe status
code.

The complete workflow still fails the RAM gate. After bounded publication,
`get_canonical_staging_run` reconstructs all canonical rows into a tuple and
`evaluate_quality` builds complete row, coordinate, dataset, issue, and result
collections. That Stage E-to-F handoff, not P3 source preparation, is the next
measured memory blocker. The later bounded direct release gate advances that
path to 50,000 rows while derived/materialized projects remain at 25,000.

The BOM scale fixture exercises BOM-shaped direct data. It does not close
Increment 4: persisted parent grouping, relationship edges, derived hierarchy,
and multi-source lineage must still be routed through the bounded session and
proved against the materializing oracle.

## Scope

This work package includes:

- bounded CSV and read-only XLSX row readers with identical validation and
  source-row numbering to the current loaders;
- a compiled, storage-independent row transformer extracted from the current
  evaluator and source preparation behavior;
- a durable preparation session and append-only batch sinks in the project
  DuckDB database;
- direct Products processing before relationship-heavy BOM processing;
- durable duplicate-identity, derived-entity, hierarchy, lineage, relationship,
  reconciliation, and deterministic-ordering state;
- transformation impacts written in batches rather than retained in one list;
- exact canonical hashing, typed-value round-trip, failure cleanup, telemetry,
  and controlled scale evidence.

This work package does not include:

- changing mapping, quality, normalization, or canonical hash semantics;
- expressing mapping rules or business decisions in SQL;
- streaming all quality and normalization evidence, which remains P4;
- raising either path to 100,000 rows, which remains P5;
- changing file-size, cell-size, formula, archive, or workbook security limits;
- adding Odoo reads or writes to preparation.

## Architectural decisions

### One semantic authority

The existing Python mapping and preparation rules remain authoritative. A
compiled row transformer accepts one validated physical row and emits typed
provisional outputs, lineage facts, identity or relationship keys, control-
total contributions, and transformation impacts.

DuckDB may persist, group, count, join explicit keys, and impose deterministic
ordering. It must not reimplement field transformations, issue policy,
relationship policy, reference meaning, or portable-value encoding.

### Bounded means bounded in Python

The configured batch size controls how many physical or provisional rows may
be resident in the reader, transformer, and repository writer. No tuple, list,
dictionary, report, or callback buffer in the new preparation path may grow in
proportion to all physical rows, canonical rows, or effects.

Global state that is genuinely required is persisted. Small compiled mapping
metadata and fixed-size counters may remain in memory. Bounded diagnostic
examples are allowed, but complete issue or impact collections are not.

### A session is not current evidence

A preparation session has a new identifier and immutable bindings to project,
source-selection hash, mapping hash, evaluator version, and portable contract
version. Its states are:

```text
BUILDING -> FINALIZING -> READY -> PUBLISHED
                    \-> FAILED
```

Rows in `BUILDING`, `FINALIZING`, `READY`, or `FAILED` sessions are never
returned as current staging evidence. The current canonical pointer changes in
one short final transaction only after all counts, hashes, lineage, and control
totals have been validated.

An implementation must use the next available project-schema migration number;
this plan does not reserve a hard-coded version while other schema work is in
progress.

### Final evidence keeps the existing contract

Final `CanonicalRow` construction, row-ID calculation, portable encoding, row
ordering, run hashing, dispositions, issue ordering, lineage ordering, and
reconciliation must remain byte-for-byte equivalent to the current path for
the same frozen inputs.

Finalization reads provisional state in the existing canonical order
`(dataset, source_row, row_id)`, constructs and encodes each final row once,
updates the incremental run hasher from those exact bytes, and appends the
same bytes to final storage. The implementation must not calculate a hash from
one representation and persist another.

### Source integrity fails closed

Registered source bytes are verified with bounded buffered reads against the
frozen selection before publication. CSV and XLSX processing may require a
bounded integrity pass separate from parsing; avoiding a second I/O pass is
not worth weakening exact byte binding.

If a source hash, mapping hash, selection hash, evaluator version, or session
binding changes, finalization stops. The previous current evidence remains
unchanged and the temporary session is marked failed for safe cleanup.

## Proposed components

Names are descriptive and may be adjusted to repository conventions, but the
boundaries are required.

| Component | Responsibility | Memory invariant |
| --- | --- | --- |
| `SelectedSourceBatchReader` | Verify and yield headers plus numbered CSV/XLSX batches | At most one input batch plus parser state |
| `CompiledRowTransformer` | Apply current Python mapping/preparation rules to one row | No retained row collections |
| `PreparationSessionSink` | Append provisional rows, facts, lineage, totals, and impacts | At most one output batch |
| `PreparationFinalizer` | Resolve global rules and stream final canonical order | Uses bounded database cursors, never `fetchall()` |
| `PreparationSessionRepository` | Own session state, batch writes, validation, publication, and cleanup | No API returns every session row |
| phase telemetry | Record durations, counts, batch high-water marks, RSS, and DB size | Counts only; no source values |

The existing `SourceTable` and `PreparedBundle` APIs remain available to small
standalone/profile workflows until their callers are migrated. The browser
preparation service must use the bounded API; it must not adapt the bounded
reader back into a complete `SourceTable`.

## Temporary durable state

The exact table names are implementation details. The schema must represent:

- one session header with immutable input bindings, state, timestamps, batch
  size, row counters, and a non-sensitive failure code;
- provisional typed output rows and their deterministic physical ordinals;
- source-identity keys used to find duplicate groups across batch boundaries;
- physical-to-output lineage facts, stored separately so parent and derived
  rows can collect many sources without a Python list;
- relationship keys and unresolved incoming references required by the
  existing governed policies;
- derived hierarchy candidates keyed by normalized canonical path;
- control-total contributions or bounded aggregates;
- pending transformation-impact rows with the same masking and access rules as
  the existing impact repository.

Temporary tables must be indexed for session plus the keys used during
finalization. They must not duplicate complete raw source rows merely for
convenience. Persist only the typed provisional values and evidence required to
reproduce the current canonical result.

Provisional JSON and typed scalar columns must use the existing portable-value
codec. Protected values must not appear in logs, failure text, telemetry, or
session status. DuckDB external access remains disabled.

## Cross-row behavior

### Direct Products path

Each selected physical row is transformed once. Its provisional canonical
row, identity key, lineage fact, control-total contribution, and effects are
appended in batches. Duplicate identity groups are resolved during
finalization, including duplicates split across input batches.

This path lands first because it proves the reader, transformer, session,
hashing, and publication boundaries without mixing in derived-entity behavior.

### BOM and related/derived paths

The second increment adds global behavior without recreating global Python
collections:

- parent candidates are keyed by the same normalized business key and scope;
- all contributing physical rows append lineage facts to that key;
- the deterministic representative remains the same lowest/first source-row
  choice used by current behavior;
- child rows retain their current one-per-source-row semantics;
- derived hierarchy paths are expanded by the Python transformer and grouped
  by canonical path in temporary state;
- aliases, relationship edges, missing keys, and ambiguous keys are finalized
  under the current policies and issue ordering;
- reconciliation counts physical rows, canonical outputs, exclusions, and
  lineage from durable facts rather than scanning `PreparedBundle`.

SQL can implement `GROUP BY`, `COUNT`, `MIN`, explicit-key joins, and `ORDER
BY` over facts already assigned meaning by Python. Any proposed SQL expression
that transforms source values or decides business policy is out of bounds.

## Implementation increments

Every increment must keep the normal suite green and record its own hypothesis.
Do not combine these into one unreviewable change.

### Increment 0 - Freeze parity evidence and phase measurements

1. Add golden Products and BOM results at small and medium sizes, including
   canonical row bytes, run hash, row IDs, issues, dispositions, lineage,
   reconciliation, totals, and effect ordering.
2. Add phase-level true working-set samples and Python batch high-water marks
   around source verification, parsing, transformation, durable append,
   finalization, publication, quality, and normalization.
3. Record the exact fixture checksum, command, revision, Python environment,
   machine, and batch size.

**Exit:** repeated current-path runs are deterministic, and instrumentation
does not expose protected values or materially affect the result.

### Increment 1 - Bounded readers and compiled transformer

1. Introduce context-managed CSV and XLSX readers that yield fixed-size
   batches while preserving headers, row numbers, blank-cell behavior,
   formulas policy, validation errors, and workbook closure.
2. Extract the current row-local transformation behavior behind the compiled
   transformer boundary without changing semantics.
3. Prove reader and transformer parity against the materializing path with
   batch sizes `1`, `17`, and the production default.

**Exit:** Products rows can be transformed in bounded memory into a test sink;
no production publication path changes yet.

### Increment 2 - Preparation session and bounded repository sinks

1. Add the next schema migration, session state machine, temporary tables,
   indexes, and append APIs.
2. Add typed bulk writes with configurable batch size and bounded cursor reads.
3. Add cancellation, failure marking, retry-as-new-session behavior, and safe
   cleanup of abandoned unpublished sessions.
4. Failure cleanup must never delete current or otherwise published runs.

**Exit:** injected failures before, during, and after a batch leave current
evidence unchanged; retry produces the same durable facts.

### Increment 3 - Stream the direct Products fixture

1. Route browser Products preparation through the bounded reader, compiled
   transformer, and session sink.
2. Spill duplicate keys, lineage, totals, and impacts instead of retaining
   them in lists.
3. Finalize rows in canonical order, stream exact encoding through hashing and
   persistence, validate the run, and atomically change the current pointer.
4. Keep the old materializing path available only as a parity oracle in tests
   until the increment passes its gates.

**Exit:** every Products golden result is byte-for-byte identical for all test
batch sizes, and the 100,000-row Products source phase has bounded memory.

### Increment 4 - Stream BOM and global/derived behavior

1. Persist parent candidate keys and grouped lineage.
2. Persist relationship edges and resolve missing or ambiguous references with
   the existing policies.
3. Persist and group derived hierarchy candidates, aliases, and source facts.
4. Produce reconciliation and effects from bounded durable iteration.

**Exit:** BOM, derived-entity, deep-relationship, fan-out, and ambiguity golden
results are identical across batch sizes and repeated runs.

### Increment 5 - Integrate, retire duplicate materialization, and measure

1. Remove browser preparation's complete `SourceTable`, `PreparedBundle`,
   `CanonicalStagingRun`, and `impact_rows` residency after the new path is the
   proven default.
2. Keep one transformation authority; delete compatibility code that would
   otherwise permit the old and new semantics to drift.
3. Run focused, full-suite, failure, security, and scale tests in fresh
   processes.
4. Update the performance plan and acceptance ledger with controlled evidence.

**Exit:** all P3 gates below pass. P4 begins from persisted canonical and
impact evidence without reintroducing source materialization.

## Correctness and failure tests

The test matrix must include:

- CSV and XLSX parity for empty cells, dates, datetimes, decimals, formulas,
  headers, row numbering, unicode, and validation failures;
- Products and BOM parity at batch sizes `1`, `17`, default, and a value larger
  than the fixture;
- duplicate identities whose first and last members occur in different
  batches;
- parent/child fan-out, repeated parents, derived hierarchy aliases, deep
  relationships, missing parents, ambiguous parents, cycles, and warning/block
  policies;
- typed logical and business references round-tripping through temporary and
  final storage;
- deterministic row IDs, run hashes, issue ordering, lineage ordering,
  dispositions, totals, and effect ordering across repeats;
- control totals with nulls, exclusions, decimals, and multiple datasets;
- source-hash mismatch before reading, during final validation, and immediately
  before publication;
- failure injection after any batch, during finalization, and before pointer
  promotion;
- cancellation, stale-session cleanup, retry, idempotent publication, and a
  concurrent binding/current-pointer conflict;
- no external DuckDB access and no protected values in logs or status records.

Structural memory tests use instrumented fake readers and sinks to assert batch
high-water marks. They supplement, but do not replace, fresh-process RSS
measurement on the real 100,000-row fixtures.

## Performance and memory gates

P3 uses phase-specific evidence so a later P4 allocation cannot be mistaken
for a source-preparation regression.

Required P3 gates:

1. Products and BOM source verification, transformation, append, and
   finalization retain no row-proportional Python collection outside the
   configured batch.
2. Changing batch size does not change any canonical or impact evidence.
3. The source-preparation phase remains below the overall 900-MiB working-set
   ceiling at 100,000 physical rows for both fixtures.
4. The retained-memory increase from 25,000 to 100,000 rows is attributable to
   bounded parser/database state, not a second complete Python object graph;
   phase counters and allocation profiles must support that conclusion.
5. Complete preparation remains below 120 seconds and is also measured against
   the 20-second source-verification and 45-second transformation/finalization
   planning budgets. Internal budgets guide diagnosis; only the 120-second
   total is a product gate.
6. Database growth is recorded and reviewed. Lower RAM is not accepted if
   temporary evidence grows without cleanup or final project size becomes
   operationally unreasonable.

The ultimate P5 gate remains stricter: the complete Products, BOM, and mixed
fixtures must each finish below 120 seconds and below 900 MiB true observed
peak in three fresh-process runs. If P3 makes the source phase bounded but the
complete workflow still exceeds 900 MiB in quality or normalization, P3 may be
closed with that evidence and P4 remains the blocker. Neither path may promise
100,000 rows before that gate passes.

## Telemetry and diagnostics

Record only:

- session and phase status;
- duration by phase;
- physical, provisional, canonical, lineage, issue, and effect counts;
- input and output batch high-water marks;
- observed peak and ending RSS;
- database and temporary-session size;
- source, selection, mapping, and output hashes where existing access policy
  permits them;
- stable non-sensitive failure codes.

Do not log source values, proposed values, credentials, numeric Odoo IDs,
business keys, cell contents, or unrestricted issue examples.

## Rollout and rollback

The bounded path may be guarded by an internal test/development switch while
parity is being established. The switch is not a permanent second product
mode. Products becomes the default only after Increment 3 passes; BOM follows
after Increment 4.

Before final publication, failure rollback is simply session failure plus
bounded cleanup; current evidence is untouched. After publication, existing
invalidation and immutable-run behavior remains authoritative. Do not add a
destructive rollback that rewrites a published run.

If any parity, security, atomicity, time, or phase-memory gate fails, retain the
old supported limit, keep current evidence unchanged, record the failed
hypothesis, and profile the responsible phase before expanding scope.

## Expected implementation areas

The work is expected to touch these areas; exact filenames may change during
refactoring:

- `src/impodo/source.py` for bounded source-reader contracts and compatibility;
- `src/impodo/domain/staging/` for the pure compiled row transformer and final
  canonical construction;
- `src/impodo/application/preparation_service.py` and readiness ports for
  session orchestration and bounded dependencies;
- `src/impodo/adapters/duckdb/` for the schema migration, session repository,
  bulk sinks, finalization queries, publication, and cleanup;
- transformation-impact persistence for pending batch append;
- focused source, staging, repository, service, security, failure, and scale
  tests;
- this plan, the parent performance plan, and the acceptance ledger.

## Definition of done

P3 is done when all of the following are true:

- Products and BOM use bounded source preparation in the browser workflow;
- the materializing browser path and complete impact list are removed;
- batch sizes produce identical portable evidence;
- failure and concurrent-change tests prove current evidence is unchanged until
  final validated promotion;
- true phase peak, ending RSS, time, database size, hashes, counts, revision,
  environment, and machine are recorded for controlled 25,000- and
  100,000-row runs;
- the full ordinary suite and opt-in scale suite pass;
- P3 status and acceptance evidence are updated without comparing measurements
  from different machines;
- the bounded direct limit remains 50,000 and the derived/materialized limit
  remains 25,000 unless the applicable P4 and P5 gates independently pass.
