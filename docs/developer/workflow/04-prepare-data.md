---
audience: developer
stage: prepare
status: current
---

# Prepare data

## Responsibility

Prepare data compiles the submitted mapping and evaluates every frozen row.
It transforms the rows admitted by the mapping, publishes canonical staging
and quality evidence, resolves ambiguous source entities, and freezes required
normalization decisions.

It is target-independent and must not contact Odoo.

## Entry conditions

The current mapping revision must have matching validation and submission
evidence. Source, schema, related-dataset plan, and mapping hashes must agree
before any publication begins.

## Implementation flow

`preparation.py` starts and monitors work through `PreparationJobManager`.
Before spawning, the route resolves and captures the Project, DataVersion,
MigrationRun, and MigrationWorkspace identities. The application also captures
the exact application build and workspace schema contract that accepted the
request.
`preparation_worker.py` composes project-only adapters and validates that
captured identity against the Project's immutable linkage; it cannot open the
shared Recipe registry. Before it opens workspace evidence, the spawned worker
must prove that it loaded the same application build and workspace schema
contract. A mismatch returns `IMPODO_BUILD_CHANGED` and requires an application
restart; the browser must not offer a blind retry. The progress page renders
from the same in-memory job snapshot. It therefore does not race either DuckDB
writer.
On terminal failure or cancellation, the progress label says **Stopped at**
the last reached percentage, and the spinner and active actions disappear.
`PreparationService` selects the supported preparation capability, compiles the
mapping, writes bounded staging batches, publishes quality/accounting evidence,
and records the preparation session.

Each direct dataset may use either a prepared-value projection or stored
canonical JSON. `PreparationQualityIndex._bounded_quality_index` accepts both
formats in the same run. It rejects a dataset whose rows mix those formats,
while the canonical reader verifies projection bindings and values. Row order,
identity uniqueness, complete source accounting, and relationship checks still
apply. Mixed storage therefore does not require whole-run materialization or
an increase to its 25,000-source-row safety limit.

Admission records each dataset's actual compiler reasons and required source
snapshot. Mixed native and Python routes are reported as `MIXED_BOUNDED`.
The report contains table identifiers and rule paths, without source values
or formula text. Compilation decisions are reused by the capacity check.

The [quality dependency rules](../../../src/impodo/domain/preparation/quality.py)
use `incoming_identity_group_fields` to derive possible complete-group dependencies
from the mapping's identity and scope components. It applies equally to
standard and custom models. Direct incoming groups now use the existing
50,000-row direct route. They no longer require the 25,000-row materialized
route solely because an incoming parent supplies their identity or scope.
Advanced rules and non-direct preparation retain their separate limits.

The [DuckDB group-quality adapter](../../../src/impodo/adapters/duckdb/preparation_identity_group_quality.py)
projects only identity, scope, and reference values from stored canonical JSON
in bounded pages, or from verified prepared columns for clean set-based rows.
It joins these facts to the existing direct source-identity index. Parent keys are counted
before matching, so duplicate keys do not multiply the join. DuckDB builds
temporary forward readiness arcs and reverse arcs for unique incoming identity
parents. A recursive distinct traversal propagates unsafe records. Warning
destinations receive findings but do not become propagation steps unless they
were already unsafe. Findings are read in bounded pages; application issue
accumulation still uses the existing compact exceptions and needs separate
error-heavy publication qualification.

These temporary facts are rebuilt from immutable evidence on each attempt.
An interrupted calculation rolls back without changing canonical rows or
requiring a storage migration. Qualified direct relational identities now use
compiler-v9 resolver metadata and bounded native Polars transformation.
Incoming, target-catalog, and target-then-incoming identities preserve ordered
composite keys, scope, aliases, and explicit null roots. Unsupported resolver
shapes retain `COLUMNAR_IDENTITY_RESOLVER_UNSUPPORTED` and use the bounded
Python route.
For native hybrid lookups and relational identities, the adapter reads
relationship, identity, and scope columns from the hash-verified prepared
artifact using canonical reference serialization. It rebuilds those facts
rather than trusting a historical edge index that may contain the incoming key
instead of the canonical matching key. Only incoming identity or scope links
propagate an unsafe child back to its parent; hybrid links remain forward
dependencies. Other native incoming links reuse the existing direct edges.
New native projections use
[`PreparedCanonicalProjection`](../../../src/impodo/domain/staging/preparation_session.py)
contract 4. Incoming,
target, and hybrid references now match the canonical Python serializer's exact
bytes, including sorted properties and omission of empty optional metadata.
The [native DuckDB projector](../../../src/impodo/adapters/duckdb/native_prepared_projection.py)
reads constant choices from the prepared literal columns. This keeps its query
valid when a linked record is supplied by
the same business key for every row.

Readers preserve projection contract 3's original reference encoding and verify
its stored content hash. Existing evidence is not migrated or rewritten. The
projection version selects serialization independently of the compiled program
hash. Tests reopen a finalized historical run, verify its complete original
hash, and reject a changed projection version against that hash. Compiler-v8
portable programs omit the new resolver metadata and retain their original
content hashes.

Clean relational identity programs use set-based DuckDB canonical projection.
Each resolved component becomes one portable logical reference; optional blank
roots remain null. SQL emits exact canonical bytes, identity hashes, labels,
and native identity-group dependencies. Nonprintable reference labels or rows
with transformation issues use the bounded canonical-payload route for the
entire dataset; the clean SQL plan never silently skips a row. Reviewed hybrid
aliases are applied after incoming-key normalization so their exact target
bytes survive. Compiler-v8 and projection-contract-3 historical artifacts
retain their original serialization. Capability diagnostics report
`RELATIONAL_IDENTITY_SCALE_UNQUALIFIED`; admission remains at 50,000 direct
rows until a larger complete route is measured and qualified.

Canonical publication parses and validates each row while hashing it. It
checks the row's coordinates, model, disposition, and mapping, schema, and
source-selection bindings against the stored index and session. Quality can
then use the exact mapping to select ordinary or complete-group checks,
without reconstructing all rows again. This decision requires matching
mapping identity, source selection, schema, and published staging hash.
Calls without the mapping retain the row-based compatibility check.

`resolution.py` applies explicit merge/separate and field-correction decisions
through `ResolutionService`. `normalization.py` handles reviewable value groups
through `NormalizationService`. Both publish new evidence rather than mutating
the frozen source.

### Columnar compilation and execution

Preparation separates mapping meaning from the engine that evaluates it.
`compile_browser_mapping` produces the shared target, identity, and relationship
semantics in `CompiledMigrationPlan`. Independently,
`compile_columnar_transformation_programs` inspects the mapping and frozen
source-selection metadata to describe native transformation work. Neither
compiler reads source rows or contacts Odoo.

For example, a rule that reads a source column, trims whitespace, converts the
result to an integer, and requires a value becomes an ordered portable program.
The program also describes validation, identities, lineage, transformation
impacts, and requirements for work across rows. Its content hash binds those
semantics without importing Polars into the domain layer.

Each `ColumnarCompilationDecision` contains either a complete supported program
or explicit fallback reasons. Qualified relational identity components carry
an additive `ColumnarIdentityResolverProgram` bound to their role and component
index. A formula, unsupported conversion, or another
unsupported operation routes the whole dataset to the bounded Python evaluator
before transformation begins. Preparation does not switch between native and
Python evaluation for individual fields or cells.

`PolarsTransformationAdapter` already implements the application-owned
`ColumnarTransformationPort`. It converts supported programs to native Polars
expressions, scans verified source Parquet, and writes a prepared snapshot
candidate. Preparation verifies and publishes that artifact and consumes its
bounded results. The shared Python evaluator supplies reference behavior for
parity tests as well as the supported fallback route.

Common arithmetic on the Python route now reuses a validated
`CompiledArithmeticFormula` instead of walking the AST for every row. The
safe parser admits numeric constants, source names, unary signs, addition,
subtraction, multiplication, and division to this instruction program. Browser
preparation compiles each formula once per dataset and builds a context from
only its referenced source columns. Profiles and previews reuse a bounded
cache. Final conversion, explicit rounding, value-choice bypasses, errors,
and transformation observations still pass through the shared scalar rules.

This is Python execution, so compiler diagnostics continue to report
`COLUMNAR_FORMULA_UNSUPPORTED` for native preparation. Functions, conditionals,
comparisons, and modulo retain the safe AST evaluator. Invalid formulas retain
row-level failures. The arithmetic instructions do not use binary floating
point or fix Decimal precision to a native storage scale. See the
[implementation diagnostics](../../plans/preparation-efficiency-and-storage.md)
for the local measurement and remaining native qualification.

Compiler support does not by itself admit a run to the high-volume route.
Full-pipeline admission also checks snapshots, dataset shape, and downstream
quality and normalization capabilities. The current limits are 100,000 physical
rows for qualified exact-snapshot, single-dataset native preparation, 50,000
for current direct Python-fallback, relationship, or native relational-identity
routes pending scale qualification, and 25,000 for derived or materialized routes.
Extending qualification belongs to the
[remaining scale work](../../plans/remaining-work.md#1-qualify-related-and-mixed-preparation-at-100000-rows).

## Code references

| Role | Code |
| --- | --- |
| Preparation orchestration | [`PreparationService`](../../../src/impodo/application/workspace/preparation/preparation_service.py) |
| Dataset capability compiler | [`compile_columnar_transformation_programs`](../../../src/impodo/domain/compiler/columnar_transformation.py) |
| Native execution contract | [`ColumnarTransformationPort`](../../../src/impodo/application/workspace/preparation/columnar_transformation_port.py) |
| Native transformation implementation | [`PolarsTransformationAdapter`](../../../src/impodo/adapters/polars_transformation.py) |
| Relational identity program | [`ColumnarIdentityResolverProgram`](../../../src/impodo/domain/compiler/columnar_transformation.py) |
| Compiled bounded arithmetic | [`CompiledArithmeticFormula`](../../../src/impodo/domain/recipe/value_rules.py) |
| Background jobs | [`PreparationJobManager`](../../../src/impodo/web/composition/preparation_job_manager.py) |
| Process build contract | [`ApplicationBuildContract`](../../../src/impodo/application/shared/build_contract.py) |
| Project-only worker wiring | [`create_preparation_worker`](../../../src/impodo/web/composition/preparation_worker.py) |
| Quality publication | [`QualityService`](../../../src/impodo/application/workspace/preparation/quality_service.py) |
| Quality indexes across direct datasets | [`PreparationQualityIndex`](../../../src/impodo/adapters/duckdb/preparation_quality_index.py) |
| Admission and dataset route diagnostics | [`compile_preparation_capability`](../../../src/impodo/application/workspace/preparation/preparation_capability.py) |
| Canonical publication validation | [`PreparationStoredRunReader`](../../../src/impodo/adapters/duckdb/preparation_stored_run_reader.py) |
| Entity resolution | [`ResolutionService`](../../../src/impodo/application/workspace/preparation/resolution_service.py) |
| Normalization decisions | [`NormalizationService`](../../../src/impodo/application/workspace/preparation/normalization_service.py) |
| Canonical hierarchy materialization | [`evaluate_browser_mapping`](../../../src/impodo/domain/staging/evaluator.py) |
| Canonical row-inclusion decision | [`canonical_row_from_inclusion_decision`](../../../src/impodo/domain/preparation/staging_contracts.py) |

## Evidence and state

Prepared evidence includes the compiled plan hash, complete canonical rows,
source-to-canonical lineage, control totals, quality findings, quarantine,
resolution state, normalization decisions, and preparation-session status.
Publication is project-scoped and hash-bound.

A version-16-or-newer `matching_rows` policy runs before target-oriented
preparation.
The bounded materialized and durable paths publish the same lineage-only
`EXCLUDED` or `BLOCKED` decisions while passing only included records to later
work. The columnar capability compiler currently routes this policy to the
bounded evaluator with `COLUMNAR_ROW_INCLUSION_UNSUPPORTED`; it does not
silently run an unverified native interpretation.

For mapping contract version 17, a generated hierarchy component carrying
`explicit_scope_null` prepares an all-blank parent key as one plain `None`
scope value without an issue or dependency edge. A populated parent remains a
`LogicalReference` and therefore a hard row dependency. Target identity keys
remain required, and partially blank composite parent keys remain invalid.

When an incoming record supplies part of a dependent row's target identity,
`evaluate_quality` treats the parent and its dependent rows as one update
group. For example, an unsafe order line sets aside its incoming order and
the other lines in that group under the default quarantine policy. The direct
bounded route preserves these same findings and dispositions, including
nested groups, multiple parent roles, and cycles. Ordinary linked fields keep
their forward dependency and do not set aside their lookup record.

For `odoo_pinned_update`, `PreparationService` verifies the one current
protected manifest and bounded origin sidecar against the source binding and
Parquet snapshot before it processes any rows. This verification requires a
constant number of local reads; it does not look up provenance for each row.
The transformation path then uses the same origin-neutral snapshot reader and
canonical staging contracts as a file source. Pinned rows intentionally have
no business identity, so duplicate grouping excludes that empty value. Source
ordinals remain ordinary lineage, while numeric Odoo IDs remain protected.

## Completion and navigation

Preparation progress uses its captured job snapshot for navigation and Recipe
context, avoiding both registry and project database reads while a writer owns
either file. Later stages remain locked while work is active or while required
resolution or normalization decisions remain. Completion requires frozen,
fully accounted prepared evidence for the current bindings.

## Invalidation and recovery

Any source, schema, mapping, compilation, derived-plan, resolution, or
normalization binding change invalidates dependent evidence. A failed or
cancelled attempt retains its status; retry creates a controlled attempt and
must not partially reuse uncommitted tables.

Recipe runs can recover an already published review after an unexpected worker
exit or loss of session state. The coordinator checks only the current eligible
application's publication bindings before restoring a terminal job snapshot.
See [integrated-run recovery](07-integrated-test-runs.md#recovering-published-preparation)
for the publication checks and concurrency guards. Status polling does not
perform this recovery, and an explicit failed or cancelled session result is
not replaced by older successful work.

A changed application build or incompatible workspace contract is deterministic
for the running process. The operator must restart Impodo or follow the
workspace compatibility action. Retrying the same job cannot repair either
condition.

After installing a preparation fix, restart Impodo and start preparation from
the same workspace's saved, confirmed mapping. A fresh attempt rebuilds the
session indexes removed by failure cleanup. It must not promote the failed
session or treat its published staging rows as a complete quality result.
The last progress percentage records the failed phase; it does not mean the
worker is still running.

Use stage-level transactions and idempotent publication. Never repair a result
by editing DuckDB rows directly.

## Odoo 19 and performance

Preparation makes zero Odoo calls. Direct transformations should remain on the
native columnar path where supported. Python fallback, derived datasets, and
relationship materialization must use bounded batches with measured memory.

Review database writes for repeated single-row `execute` calls and source-row
loops for hidden conversions. New paths must preserve deterministic hash and
lineage parity before being called an optimization.

## Verification

- [`tests/application/workspace/preparation/test_capability.py`](../../../tests/application/workspace/preparation/test_capability.py)
- [`tests/domain/recipe/test_columnar_compiler.py`](../../../tests/domain/recipe/test_columnar_compiler.py)
- [`tests/domain/recipe/test_arithmetic_formula.py`](../../../tests/domain/recipe/test_arithmetic_formula.py)
- [`tests/domain/preparation/test_browser_arithmetic_formula.py`](../../../tests/domain/preparation/test_browser_arithmetic_formula.py)
- [`tests/integration/columnar/test_polars_transformation.py`](../../../tests/integration/columnar/test_polars_transformation.py)
- [`tests/application/workspace/preparation/test_jobs.py`](../../../tests/application/workspace/preparation/test_jobs.py)
- [`tests/architecture/test_build_contract.py`](../../../tests/architecture/test_build_contract.py)
- [`tests/architecture/test_workspace_schema_contract.py`](../../../tests/architecture/test_workspace_schema_contract.py)
- [`tests/integration/duckdb/test_preparation_session.py`](../../../tests/integration/duckdb/test_preparation_session.py)
- [`tests/integration/duckdb/test_identity_group_quality.py`](../../../tests/integration/duckdb/test_identity_group_quality.py)
- [`tests/integration/duckdb/test_native_reference_serialization.py`](../../../tests/integration/duckdb/test_native_reference_serialization.py)
- [`tests/domain/preparation/test_quality.py`](../../../tests/domain/preparation/test_quality.py)
- [`tests/domain/preparation/test_normalization.py`](../../../tests/domain/preparation/test_normalization.py)
- [`tests/performance/test_preparation_scale.py`](../../../tests/performance/test_preparation_scale.py)
- [`tests/performance/test_preparation_identity_groups.py`](../../../tests/performance/test_preparation_identity_groups.py)
- [`tests/integration/web/test_preparation_workflow.py`](../../../tests/integration/web/test_preparation_workflow.py)
- [`tests/application/workspace/preparation/test_readiness.py`](../../../tests/application/workspace/preparation/test_readiness.py)
- [`tests/integration/artifacts/test_source_snapshot_io.py`](../../../tests/integration/artifacts/test_source_snapshot_io.py)

Verify atomic rollback, cancellation, retry, bounded memory, complete
accounting, deterministic hashes, lineage, progress rendering under real
cross-process DuckDB locks, and the appropriate scale gate for each execution
class.

## Related documentation

- [User guide: Prepare data](../../user/workflow/04-prepare-data.md)
- [Canonical staging contract](../contracts/canonical-staging.md)
- [Normalization governance contract](../contracts/normalization.md)
- [Quality and quarantine contract](../contracts/quality-and-quarantine.md)
