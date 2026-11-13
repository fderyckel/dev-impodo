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

Preparation itself is target-independent and must not contact Odoo. After the
prepared result is approved, the Stage 4 page may hand the workspace to the
single read-only comparison owned by Final review.

## Contextual user guidance

The affected-records finding and prepared-data approval headings link to the
relevant user-guide sections. The shared
[guide-help contract](00-project-setup.md#data-manager-concept-and-guide-help)
owns these static links and their coverage checks.

## Entry conditions

The current mapping revision must have matching validation and submission
evidence. Source, schema, related-dataset plan, and mapping hashes must agree
before any publication begins. The source-stage readiness gate must also be
current. For an Odoo source, the schema and capture-plan model sets must match
and a complete frozen source selection must exist.

For a file workspace with saved supporting choices, preparation also rechecks
their input binding and agreement with the submitted mapping before evaluating
rows. `SupportingMappingGuard.require_preparation` reuses the operation's loaded
workspace, physical selection and preparation rules. The local and spawned
worker compositions inject the same policy through ports. Odoo-source decisions
continue through their existing provenance and relationship-scope checks.

## Implementation flow

`preparation.py` starts and monitors work through `PreparationJobManager`.
The route returns stale requests to the earliest unfinished source page.
`enqueue_preparation` repeats the same derived source-stage check before it
creates or retries a job, so non-browser callers cannot publish a zero-row
failure merely to discover that no current source version exists.
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

Quality publication also derives the complete-run cause counts after the
batched issue rows have been stored. Inherited issues retain one causal row
identity and a parent-or-child direction. `QualityRepository` performs one
recursive set operation inside the publication transaction: it groups direct
causes, walks the persisted causal links, and counts distinct parents,
children, and siblings made unsafe by each cause. It stores the compact result
in `quality_evidence_projection` and the value-free run/cause/row/impact
membership in `quality_cause_member`.

The projection keeps the dataset, stable reason, representative message, rule
family, outcome policy, correction owner, affected fields, direct affected
record count, and the three dependency-impact counts. Numeric-capacity causes
also keep captured total digits and scale, the maximum required decimal places,
and counts for integral overflow or a positive value that could become zero.
It contains no source or prepared value. Equivalent messages that differ only
in row-specific detail are counted once. The preparation owner therefore scans
and closes the issue graph once; browser pages do not repeat it or load every
issue into Python. A current browser read asks for the projection through the
existing bounded quality-page query. Historical projections without the
enriched counts or membership index remain readable and are not rewritten.

Stages 4 and 5 project those counts through the shared read-only
`WorkflowIssueProjection`. The projection orders root findings before inherited
dependencies, shows the authoritative policy and owner when current evidence
contains them, shows the parent, child, and sibling impact when non-zero, and
builds one non-mutating Match-data route. The route filters and anchors the
current dataset's scalar field, relationship, Odoo identity, or guided
data-check decision when the saved schema can identify it. This does not create
a second issue store and adds no page-read query. A numeric-capacity card opens
the exact scalar rule and offers a second non-mutating route to the captured
Odoo field precision.

**Review affected records** uses the stable cause key from that projection.
`get_quality_cause_review_page` filters the publication-time membership index,
orders by dataset and source row, and reconstructs only one bounded page with
its saved issues. Sparse clean-row defaults and stored exceptions retain their
existing meaning. The route runs as one bounded synchronous page operation;
it does not recurse over causal links, rescan prepared values, contact Odoo, or
introduce a cross-request DuckDB connection cache.

Before quality evaluation, `PreparationService` validates the local schema or
remote worker precision packet against the submitted mapping hash. It derives
only non-reference, writable fields with captured `digits`: validation-only
mappings and values left to an Odoo default are excluded. The full evaluator checks the exact
post-transformation Decimal through the
[numeric-capacity domain](../../../src/impodo/domain/target_numeric_precision.py).
The bounded evaluator performs the same check through
`PreparationQualityIndex`: stored JSON uses a targeted JSON relation, while
clean native datasets project only the selected prepared columns from the
hash-verified Parquet snapshot. Both routes publish the same value-free
`ADVISORY` issue and leave the row eligible. They never round, contact Odoo, or
load a whole native canonical row merely to check precision.

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
Advanced rules and most non-direct preparation retain their separate limits.
The bounded exception is one non-hierarchical `DerivedEntityRule` whose exact
effective selection adds one generated table with no more than 5,000 records.
Without advanced quality rules or reference data, that route admits 50,000
physical rows and carries the selected materialized ceiling through the domain
evaluator, quality fallback, normalization, and relationship checks. Other
non-direct routes outside the reviewed exact-join graph retain the 25,000-row ceiling.

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

Relationship projection retains the parent dataset and affected target field.
When propagation starts from an unsafe parent, it also retains one
deterministic related row. `evaluate_quality`, the materialized bounded
fallback, the direct relationship index, and the identity-group index use the
same reason builder. Missing, ambiguous, forward set-aside, and reverse
identity-group conditions therefore remain distinct without changing the
portable quality contract shape. Evaluator version 4 invalidates earlier
current quality runs rather than rewriting their evidence.

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
Native projections require
[`PreparedCanonicalProjection`](../../../src/impodo/domain/staging/preparation_session.py)
contract 4. Incoming,
target, and hybrid references now match the canonical Python serializer's exact
bytes, including sorted properties and omission of empty optional metadata.
The [native DuckDB projector](../../../src/impodo/adapters/duckdb/native_prepared_projection.py)
reads constant choices from the prepared literal columns. This keeps its query
valid when a linked record is supplied by
the same business key for every row.

Readers accept only the exact current integer encoding version, defined by
`PREPARED_CANONICAL_ENCODING_VERSION`. An unsupported saved projection raises
`PreparedCanonicalEncodingUnsupported` and reports that the data must be
prepared again. Impodo preserves the original source and saved rules; it does
not migrate or reinterpret the old prepared rows. The projection version is
checked independently of the compiled program hash. Tests verify current
serialization and reopening, and reject an unsupported saved encoding.

Clean relational identity programs use set-based DuckDB canonical projection.
Each resolved component becomes one portable logical reference; optional blank
roots remain null. SQL emits exact canonical bytes, identity hashes, labels,
and native identity-group dependencies. Nonprintable reference labels or rows
with transformation issues use the bounded canonical-payload route for the
entire dataset; the clean SQL plan never silently skips a row. Reviewed hybrid
aliases are applied after incoming-key normalization so their exact target
bytes survive. Capability diagnostics report
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

For the bounded normalization route, Impodo resolves each mapped field's review
policy and builds its group metadata once per evaluation. The cache belongs to
that evaluation and is bounded by its compiled policy fields. Every effect
retains its own row identity, protected displays, eligibility, and exact effect
hash. Each returned metadata dictionary is independent, so a consumer
cannot alter the cached group.

The DuckDB normalization adapter also sends each distinct group seed once per
effect batch. Its key includes the group identifier and all eight normalized
metadata fields. Conflicting metadata therefore remains visible to the existing
SQL consistency check, even when effects share an identifier. The adapter
clears the seed dictionary at each batch boundary. Canonical JSON encoding,
effect ordering, duplicate handling, transaction rollback, and canonical
eligible-data hashing retain their existing contracts.

When stored staging provides `CanonicalHashRowSource`, normalization can hash
verified native canonical JSON without restoring each row again. Current
JSON-backed rows are restored one at a time after their ordinal is checked,
before eligibility is tested. Only eligible restored rows are serialized into
the hash. Both paths preserve the same canonical object framing, row identities,
membership checks, and staging and quality bindings. Resolved effective
datasets continue to supply their canonical rows through the existing
iterator. The hashing capability does not change the exact encoded stream used
to publish staging evidence.

### Handoff to the shared Odoo comparison

After normalization approval, `workspace_normalization.html` exposes
**Compare with Odoo** through the existing `/summary/compare` route. That
action calls the same `PreflightService.compare` workflow used from Final
review. Stage 4 does not implement another target-reference checker, save a
second target snapshot, or compare during page rendering. Opening Final review
therefore reuses the current saved comparison when its prepared and target
bindings remain current.

Preparation still completes without Odoo. An unavailable target leaves the
approved prepared result intact and the comparison incomplete. Source-only
quality and incoming-parent failures remain preparation responsibilities.
Preparation now advises when any exact mapped prepared value exceeds captured
numeric capacity; Final review remains responsible for deciding whether an
actual intended write is blocked, as unchanged values are not writes.

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
[implementation diagnostics](../../plans/11-preparation-efficiency-and-storage.md)
for the local measurement and remaining native qualification.

Compiler support does not by itself admit a run to the high-volume route.
Full-pipeline admission also checks snapshots, dataset shape, and downstream
quality and normalization capabilities. The current limits are 100,000 physical
rows for qualified exact-snapshot native preparation, including direct
relationships and relational identities. An otherwise supported direct mapping
whose only compiler fallback is `ROW_INCLUSION` also receives this limit when
every frozen snapshot matches. That route retains the shared Python admission
evaluator and reports `BOUNDED_PYTHON` with `PARQUET_ROW_INCLUSION`; it does not
claim native execution. Other unsupported direct transformations retain
50,000 rows. The limit is also 50,000 for one low-cardinality
single-column lookup-derived table without advanced quality or reference data,
and 25,000 for other derived or materialized routes.
Reviewed exact-join graphs use the separate composed route below. Extending
the remaining direct and general derived qualification belongs to the
[remaining scale work](../../plans/00-remaining-work.md#1-qualify-related-and-mixed-preparation-at-100000-rows).

Input validation, direct transformation, canonical publication, quality and
normalization each reuse local database owners within one synchronous phase.
Owners close before the next phase or a failed retry. Each repository keeps
its own transaction, and jobs do not share connections. The
[full-service regression](../../../tests/integration/artifacts/test_row_inclusion_preparation.py)
checks failure cleanup and phase boundaries. The
[capacity tests](../../../tests/application/workspace/preparation/test_row_inclusion_capacity.py)
retain the lower limit for missing snapshots and other unsupported operations.
The [scale qualification](../../../tests/performance/test_row_inclusion_preparation_scale.py)
checks 54,191 and 100,000 rows through admission, row review, preparation,
quality and normalization, with fixed connection and schema budgets and a
bulk-transport SQL budget. The isolated fixture performs 39 connections and
28 schema validations at both sizes; source rows never select one database
connection per record.

### Joined relationship matches and Recipe reuse

[`bounded_preparation_producers`](../../../src/impodo/application/workspace/preparation/grouped_exact_join_preparation.py)
qualifies each effective dataset independently.
An unchanged physical table, a flat reusable list, or a complete reviewed exact
LEFT join group can feed the same durable session. Several independent groups
can share physical inputs. Every output must qualify; unsupported producers retain the guarded
materialized route rather than silently disabling a required check.

Hierarchies built from several fields retain the guarded materialized route.
For a combined table, the evaluator recovers the hierarchy levels from each
row's physical source lineage, even when the combined projection omits them.
Reconciliation counts all distinct physical input tables in a structural
chain, so its input count covers the complete lineage rather than only the
final join's immediate inputs. The
[`combined hierarchy workflow tests`](../../../tests/integration/web/test_combined_hierarchy.py)
verify those counts and the resulting category references through load and
reconciliation.

Browser evaluator 10 publishes this combined-hierarchy and physical-input
accounting behavior. Evaluator-7, evaluator-8, and evaluator-9 staging remains
readable with its original hashes; newly published staging uses evaluator 10.

`prepare_bounded_grouped_exact_join_session` compiles the mapping once, retains
evidence hashes outside its row loop, and appends each producer in stable
dataset-name order. It finalizes one session after all outputs, control totals,
impacts, and lineage are complete. Pure direct preparation retains its existing
native Polars path. Direct tables inside a composed graph use bounded row-local
transformation from frozen Parquet.

`DuckDbGroupedExactJoinSource.iter_lookup_batches` delegates flat reusable-list
production to [`iter_lookup_batches`](../../../src/impodo/adapters/duckdb/lookup_preparation_source.py).
It scans only the immutable source ordinal and lookup value columns into a
temporary relation, then normalizes each distinct value once through the
authoritative Unicode path function. Bounded JSON pages carry normalized
values back into DuckDB for set-wise membership joins. Output keys, first-seen
labels, alias findings, and complete physical lineage match materialized
lookup evaluation. Cardinality and encoded-lineage budgets remain enforced.
Joined producers project only columns used by the mapping and their join
keys; formulas conservatively retain the complete input column set.

Existing Many2one requiredness governs empty links to generated lists. An
all-blank optional key emits a null reference without a generated-list blank
finding. Populated invalid paths and partly populated keys still follow their
checks. `required_on_create` remains a preflight creation constraint, so it
does not quarantine existing records merely because their source link is
empty. Matching, validation, and Recipe reuse keep these flags while Stage 1
stays unchanged. Browser evaluator 9 publishes this meaning; historical
evaluator-7 and evaluator-8 staging remain readable without rehashing.

The composed route has no physical-row admission cutoff. The capability
manifest reports `supported_rows=None` and volume advisories above 50,000 rows.
It still rejects stale reviews and bindings. DuckDB owners are retained only
within separate synchronous accounting and producer operations, with no await
or Odoo call inside either scope. Source-row accounting scans the ordinal column
once per physical snapshot. Each group separately scans only its required keys
and output columns once per participant, using 256 MB and two threads. Canonical
pages contain at most 1,000 joined rows and database transport retains its byte
budgets. The quality finding index has a 64 MiB encoded-finding budget before
Python collection; exceeding it fails without whole-run fallback.

The quality index verifies arbitrary physical lineage rather than requiring one
physical row per output. Its accounting projection groups all canonical links
for each physical coordinate and pages in key order. Shared source rows count
once. Unused additions in an accepted LEFT join remain `EXCLUDED_BY_RULE`;
unexplained loss on other routes remains blocking. Quality evaluator 8 records
this policy for graphs that also contain flat reusable lists. Evaluator-6 and
evaluator-7 evidence remain readable with their original hashes.

A reviewed grouped exact join uses the bounded join reader during preparation.
The relationship compiler applies each saved Many2one value match to that
logical row and retains a portable related-model key in canonical staging.
Physical lineage still identifies the contributing accepted source rows.
Preparation does not fetch target records or resolve numeric Odoo IDs.

Recipe publication retains the logical join group and explicit value matches.
Application creates fresh rule and group identities for the new delivery.
`DerivedEntityWorkspaceService.save_derived_entity_plan` reviews every group
against the new immutable snapshots before the plan is saved. The review uses
one bounded DuckDB operation and scans each participating snapshot once.
Current categorical validation checks the rebuilt logical output; a new
unmatched choice blocks the application before preparation.

The fictional three-input fixture contains 65,299 physical rows and 262 source
fields, including empty and colliding fields. Two combined outputs produce
9,506 eligible rows with incoming references, external aliases, and intentional
blanks. The real worker, approval, Final review, and fresh-file Recipe reuse are
covered by [`test_mixed_join_preparation.py`](../../../tests/integration/web/test_mixed_join_preparation.py).
The suite also qualifies 100,000 inputs and a failed or cancelled repeat that
preserves the preceding complete result. Joined error findings and captured
Odoo snapshots have differential quality and complete-lineage tests.
[`test_grouped_exact_join_preparation.py`](../../../tests/integration/artifacts/test_grouped_exact_join_preparation.py)
compares complete quality evidence and tests the finding budget.
[`test_mixed_preparation.py`](../../../tests/e2e/test_mixed_preparation.py)
checks the advisory, preparation, approval, and Final review in Chromium.

Hierarchy cause navigation is covered by
[`test_hierarchy_causes.py`](../../../tests/integration/web/test_hierarchy_causes.py)
and [`test_hierarchy_review.py`](../../../tests/e2e/test_hierarchy_review.py).
The HTTP suite follows saved correction links, saves reviewed component and
quantity columns through Match data, and confirms the corrected mapping.
Repreparation releases the complete BoM group while preserving earlier quality
evidence, including after the application reopens. The suite also rejects
unavailable causes and denies evidence access before the cause query runs.

The summary uses the captured schema in both LOCAL and REMOTE modes to locate
the owning field. It resolves a transformed value's internal field identifier
through that dataset's mapping before building the correction link. This
projection reads saved evidence without changing a prepared value or hash.
[`test_summary_presenter.py`](../../../tests/integration/web/test_summary_presenter.py)
also verifies that two datasets using the same model retain their own field
owners. [`test_hierarchy_failures.py`](../../../tests/integration/web/test_hierarchy_failures.py)
qualifies duplicate, missing, quarantined, and deliberately excluded parents
from separately accepted category and Product files.
The real BoM summary fixture stays within six physical connections and one
schema check. Its cause-detail page stays within five connections and five
schema checks. Both routes run inside their existing bounded page-read scope.

[`test_join_lookup_preparation.py`](../../../tests/integration/web/test_join_lookup_preparation.py)
qualifies the same 65,299-input shape with a reviewed combination and a flat
generated list. Empty source cells keep optional links empty through real
worker preparation, approval, Final review, and fresh-file Recipe reuse.
[`test_lookup_preparation_source.py`](../../../tests/integration/artifacts/test_lookup_preparation_source.py)
compares Unicode grouping and every source member against the materialized
oracle. It also bounds database opens and proves normalization occurs once
per distinct value.

On macOS, six preparations in three fresh worker processes measured 4.08–4.63
seconds and 349–511 MiB sampled peak worker RSS. Each producer phase used seven
physical connections, three schema checks, five narrow join-input projections,
and three ordinal scans, with no lock retries or whole-run reconstruction.
Staging and quality hashes were identical across all attempts. These are local
fictional-fixture measurements under concurrent test load, not live workspace
latency or Linux release qualification. Reproduce them with
[`benchmark_mixed_preparation.py`](../../../scripts/benchmark_mixed_preparation.py)
and `--runs 3 --output <temporary-result.json>`.

The producer uses the same frozen snapshot contract for FILE and ODOO inputs.
Preparation still verifies protected Odoo capture provenance before reading.
Portable server composition and runtime coordination tests cover the existing
server boundary; this change does not qualify a deployed Linux/PostgreSQL
session or enable unfinished remote-server capabilities.

Explicit `blank_source_values` remain part of the rebound mapping. The shared
row evaluator and native Polars key projection produce null relationships for
those values, and supporting reads omit the corresponding target-key probes.

[`test_joined_recipe_preparation.py`](../../../tests/integration/web/test_joined_recipe_preparation.py)
qualifies authoring preparation, stored Recipe publication, fresh-file Test run
activation, and preparation of the rebound mapping for custom models. It also
checks changed source keys, retained aliases, fresh review counts, complete
physical lineage, blocked new choices, and the review connection count.

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
| Bounded native thread configuration | [`configure_preparation_runtime`](../../../src/impodo/application/shared/preparation_runtime.py) |
| Execution-time resource observations | [`PreparationPerformanceObservation`](../../../src/impodo/web/composition/preparation_performance.py) |
| Preparation performance sanitation | [`sanitize_performance_event`](../../../src/impodo/web/preparation_performance_diagnostics.py) |
| Performance diagnostic ZIP members | [`performance_bundle_members`](../../../src/impodo/web/performance_bundle.py) |
| Process build contract | [`ApplicationBuildContract`](../../../src/impodo/application/shared/build_contract.py) |
| Project-only worker wiring | [`create_preparation_worker`](../../../src/impodo/web/composition/preparation_worker.py) |
| Quality publication | [`QualityService`](../../../src/impodo/application/workspace/preparation/quality_service.py) |
| Complete-run quality causes | [`QualityCauseCount`](../../../src/impodo/domain/preparation/quality.py) and [`_read_complete_quality_cause_counts`](../../../src/impodo/adapters/duckdb/quality_repository.py) |
| Shared issue projection and exact correction route | [`WorkflowIssueProjection`](../../../src/impodo/application/workspace/issues.py) and [`_quality_workflow_issue_summary`](../../../src/impodo/web/presenters/summary.py) |
| Quality indexes across direct datasets | [`PreparationQualityIndex`](../../../src/impodo/adapters/duckdb/preparation_quality_index.py) |
| Actionable relationship reason builder | [`_relationship_issue_details`](../../../src/impodo/domain/preparation/quality.py) |
| Admission and dataset route diagnostics | [`compile_preparation_capability`](../../../src/impodo/application/workspace/preparation/preparation_capability.py) |
| Canonical publication validation | [`PreparationStoredRunReader`](../../../src/impodo/adapters/duckdb/preparation_stored_run_reader.py) |
| Canonical publication and format-aware reuse | [`StagingRepository.publish_canonical_staging`](../../../src/impodo/adapters/duckdb/staging_repository.py) |
| Resolution publication and parent binding | [`AdvancedCoverageRepository.publish_resolution_evaluation`](../../../src/impodo/adapters/duckdb/advanced_coverage_repository.py) |
| Canonical eligible-data hashing | [`canonical_eligible_dataset_hash`](../../../src/impodo/domain/preparation/normalization.py) and [`CanonicalHashRowSource`](../../../src/impodo/domain/staging/preparation_session.py) |
| Current prepared-result recovery | [`PreparationRecoveryRepository`](../../../src/impodo/adapters/duckdb/preparation_recovery_repository.py) |
| Preparation review and command authority | [`PreparationRecoveryService`](../../../src/impodo/application/workspace/preparation/recovery.py) |
| Recipe preparation admission and recovery | [`enqueue_preparation`](../../../src/impodo/web/run_commands.py) and [`recover_run_preparation`](../../../src/impodo/web/run_commands.py) |
| Current attempt selection | [`PreparationJobRegistry.invalidate_result`](../../../src/impodo/application/workspace/preparation/preparation_job_registry.py) |
| Entity resolution | [`ResolutionService`](../../../src/impodo/application/workspace/preparation/resolution_service.py) |
| Normalization decisions | [`NormalizationService`](../../../src/impodo/application/workspace/preparation/normalization_service.py) |
| Bounded normalization groups and timing | [`build_bounded_normalization_evaluation`](../../../src/impodo/application/workspace/preparation/bounded_normalization.py) |
| Durable normalization facts and batched group seeds | [`PreparationNormalizationRecords`](../../../src/impodo/adapters/duckdb/preparation_normalization_records.py) |
| Approved-data comparison handoff | [`workspace_normalization.html`](../../../src/impodo/web/templates/workspace_normalization.html) and [`PreflightService.compare`](../../../src/impodo/application/preflight_service.py) |
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

`PreparationRecoveryRepository` checks each saved projection's encoding version
in the same bounded query that reads publication headers. A missing, malformed,
non-integer, or unsupported encoding prevents the saved result from being
offered as resumable. This check reads projection metadata without restoring
canonical rows. Fresh preparation publishes a current result from the accepted
source and saved rules.

Staging publication reuses matching content only while the saved encoding is
current. Otherwise, fresh preparation publishes a new staging run and retains
the earlier evidence. Resolution publication requires both the same evaluation
hash and the same `staging_run_id` before returning an existing result. A new
parent staging run follows the existing resolution invalidation and publication
path, even when its canonical values are unchanged.

Authoring and Recipe navigation verify this current result before offering
duplicate or normalization review. Read-only navigation calls
`PreparationRecoveryService.current_for_review` with `PROJECT_VIEW` authority.
Preparation commands use `current`, which still requires `MAPPING_SUBMIT`.
Both methods use the same compact publication check; viewing the page does not
start a preparation worker.

If a Recipe's result is unavailable, its earlier terminal attempts remain in
history but no longer select the current review action. A stale observation
cannot hide an active or newer attempt. A result can become available again
only after a fresh publication check. An explicit preparation command may move
the Recipe from `PREPARED` to `RUNNING` after the existing run, source, review,
schema, and mapping checks pass. The transition also binds the expected mapping
hash. Local database owners close before worker dispatch.

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

## Odoo compatibility and performance

Preparation makes zero Odoo calls. Direct transformations should remain on the
native columnar path where supported. Python fallback, derived datasets, and
relationship materialization must use bounded batches with measured memory.

### Bounded native threads and performance evidence

The local preparation worker supports an explicit Polars thread limit on
Python 3.12. Set `IMPODO_PREPARATION_THREADS` to `1`, `2`, or `4` before
starting Impodo. The default remains one native thread. When that setting is
absent, the worker preserves an existing positive `POLARS_MAX_THREADS` value.
`configure_preparation_runtime` applies the selected value inside the child
and checks the actual `polars.thread_pool_size()` before preparation opens its
workspace stores. Invalid settings or an already initialized pool with a
different size stop the attempt with an explicit configuration failure.
The parent process keeps its existing environment and native pool.

The local launcher still starts one preparation process by default. This
setting changes the Polars pool within that process; it does not increase the
preparation process count, change DuckDB thread limits, or contact Odoo.
`server_supervisor.py` imports the local application composition only when the
server child starts. Spawn replay can therefore reach the preparation runtime
configuration before application adapters initialize a native pool.

`PreparationPerformanceObservation` records execution-time build and runtime
versions, operating-system and CPU architecture, available CPU and memory,
requested and effective thread counts, and the workload shape. Workload
counts and route classifications reuse the preparation capability inputs;
collecting them adds no source scan or repository read. The worker also
returns aggregate DuckDB connection, schema-check, and lock-wait observations.

The existing supervisor loop samples parent and worker resident memory at
most once per second. These are sampled RSS observations, not exact peaks or
hard process-memory limits. Combined RSS is the sum from one sample and can
count shared pages twice. Queue wait, process startup, child phases, nested
subphases, and terminal duration retain their scope. Inclusive and nested
durations overlap and must not be added together. Collection failures remain
separate from the saved preparation outcome.

On the bounded normalization route, `normalization_effect_aggregation` measures
construction and aggregation of the durable effect and finding facts.
`normalization_eligible_hash` measures the canonical eligible-data hash. Both
are nested within `normalization_aggregation`; that enclosing measurement also
includes policy and review-group construction. These separate durations help
identify which work dominates an attempt without changing the evidence being
hashed.

The existing diagnostic ZIP includes `performance.json` and
`performance-summary.txt`. These members contain bounded, sanitized
attempt-correlated records and explicit missing-history, sampling, and
terminal coverage. They retain no source values, file paths, host names,
credentials, or arbitrary environment variables. The existing Stage 5
`performance-summary.json` member keeps its separate comparison evidence.
A Windows user's normal preparation can supply useful observations through
the same user-initiated diagnostic export. A single observation does not prove
a speedup or qualify Windows performance.

The additional attribution sections reuse the same capture and export path.
[`sanitize_file_io`](../../../src/impodo/web/performance_attribution.py) owns
the artifact-cost export schema.
`file_io` counts existing artifact reads, writes and hash passes and records
their elapsed time. Hash time includes its file read; it is not an independent
CPU measurement or physical disk throughput. No extra read is performed to
collect a byte count. Requests also retain source-validator and inspector
startup intervals. Parent durations use the parent clock; child build costs
are durations returned by the child, never differences between process clocks.
The preparation phases `process_start_call`, `worker_first_message_wait`,
`worker_build_hash`, `worker_runtime_initialization` and `worker_composition`
overlap the existing broad `worker_startup` interval.

Each captured dataset has an opaque identity, row count, predicted route,
observed evaluator and adaptation route, stable reason codes, and outcome.
Dataset elapsed time covers evaluation and canonical adaptation, excluding
later run-wide publication and quality. Repeated snapshots replace earlier
ones, so their rows must not be added together. Capture is limited to 128
datasets and 24 KiB, with at most eight reasons per dataset and explicit
omissions. A predicted route never substitutes for an unobserved execution
route. Compiler messages, source names, field names and formulas are excluded.
[`DatasetPreparationPerformance`](../../../src/impodo/application/workspace/preparation/fallback_performance.py)
owns those replacement snapshots.

Numeric capacity checks stream the selected numeric cells through the existing
Python precision predicate. They publish value-free failures through bounded
set-based batches. The input grouping targets 256 cells and 2 MiB; a single
larger cell is permitted only within the existing finding envelope. The
64 MiB retained-finding allowance is shared across indexed findings and the
consumer's retained issue metadata. It is a retained-evidence budget, not a
hard process-RSS limit. Excess findings stop the check before a successful
result is published. Numeric observations report actual cells, findings,
page and batch sizes, elapsed time and capture failures. This does not qualify
new native formulas or increase the existing route limits.
[`bounded_numeric_precision_rows`](../../../src/impodo/adapters/duckdb/preparation_numeric_precision.py)
owns the streamed evaluation and failure transport.

`performance-summary.json` includes logging measurements from the exporting
recorder's memory. These measure encoding, lock waits, output handling and
request lifecycle through the terminal log write. Existing request
`duration_ms` retains its original meaning. Recorder totals and UUID
correlations overlap and are not additive; child-process logging coverage is
unavailable. Exporting an observation does not generate another observation
of that export. Missing capture and failures remain distinct from zero cost.
Antivirus attribution remains unavailable without separate authorized system
evidence.

The opt-in [preparation worker benchmark](../../../scripts/benchmark_preparation_workers.py)
starts fresh production workers and checks actual first and repeat thread
counts and application builds. `--thread-matrix` rotates the `1`, `2`, and `4`
settings across repetitions and reports each configuration separately. For
example, from the repository root with the checkout's selected `uv` runner:

```powershell
uv run --frozen --group quality python scripts/benchmark_preparation_workers.py --thread-matrix --runs 3 --rows 10000 --columns 30 --mapped-fields 20
```

The harness requires fixed source, identical fixtures, stable result hashes,
matching runtime versions, and worker exit before accepting a comparison.
It separates first preparation from repeat use of the immutable prepared
snapshot and uses sampled RSS on both macOS and Windows. Its default requires
a clean candidate; `--allow-dirty-worktree` permits diagnostic runs without
turning them into qualification evidence. Measure collection overhead against
the same workload and report actual macOS and Windows evidence separately.
Retain the default until repeatable end-to-end gains, memory behavior, and
correctness justify changing it.

Review database writes for repeated single-row `execute` calls and source-row
loops for hidden conversions. New paths must preserve deterministic hash and
lineage parity before being called an optimization.

The cause and issue projections add no browser database owner or schema check.
Focused tests require one physical connection and one schema check for a real
quality-page read inside `retain_databases_for_read`. The summary route also
proves that its quality read runs off the event loop and remains within the
existing eight-connection request ceiling. In the shared-server composition,
the data-plane worker publishes this bounded projection with its verified
operation result; an application request must not reopen worker-owned DuckDB.

## Verification

- [`tests/application/workspace/preparation/test_capability.py`](../../../tests/application/workspace/preparation/test_capability.py)
- [`tests/domain/recipe/test_columnar_compiler.py`](../../../tests/domain/recipe/test_columnar_compiler.py)
- [`tests/domain/recipe/test_arithmetic_formula.py`](../../../tests/domain/recipe/test_arithmetic_formula.py)
- [`tests/domain/preparation/test_browser_arithmetic_formula.py`](../../../tests/domain/preparation/test_browser_arithmetic_formula.py)
- [`tests/integration/columnar/test_polars_transformation.py`](../../../tests/integration/columnar/test_polars_transformation.py)
- [`tests/application/workspace/preparation/test_jobs.py`](../../../tests/application/workspace/preparation/test_jobs.py)
- [`tests/application/workspace/preparation/test_thread_runtime.py`](../../../tests/application/workspace/preparation/test_thread_runtime.py)
- [`tests/application/workspace/preparation/test_worker_performance_integration.py`](../../../tests/application/workspace/preparation/test_worker_performance_integration.py)
- [`tests/application/workspace/preparation/test_performance_cleanup.py`](../../../tests/application/workspace/preparation/test_performance_cleanup.py)
- [`tests/integration/web/test_preparation_performance_observation.py`](../../../tests/integration/web/test_preparation_performance_observation.py)
- [`tests/integration/web/test_preparation_performance_diagnostics.py`](../../../tests/integration/web/test_preparation_performance_diagnostics.py)
- [`tests/integration/web/test_performance_attribution.py`](../../../tests/integration/web/test_performance_attribution.py)
- [`tests/integration/web/test_diagnostic_costs.py`](../../../tests/integration/web/test_diagnostic_costs.py)
- [`tests/integration/web/test_preparation_dataset_routes.py`](../../../tests/integration/web/test_preparation_dataset_routes.py)
- [`tests/integration/duckdb/test_numeric_precision_streaming.py`](../../../tests/integration/duckdb/test_numeric_precision_streaming.py)
- [`tests/performance/test_preparation_worker_benchmark.py`](../../../tests/performance/test_preparation_worker_benchmark.py)
- [`tests/architecture/test_build_contract.py`](../../../tests/architecture/test_build_contract.py)
- [`tests/architecture/test_workspace_schema_contract.py`](../../../tests/architecture/test_workspace_schema_contract.py)
- [`tests/integration/duckdb/test_preparation_session.py`](../../../tests/integration/duckdb/test_preparation_session.py)
- [`tests/integration/duckdb/test_identity_group_quality.py`](../../../tests/integration/duckdb/test_identity_group_quality.py)
- [`tests/integration/duckdb/test_native_reference_serialization.py`](../../../tests/integration/duckdb/test_native_reference_serialization.py)
- [`tests/domain/preparation/test_eligible_hash_stream.py`](../../../tests/domain/preparation/test_eligible_hash_stream.py)
- [`tests/integration/duckdb/test_eligible_hash_stream.py`](../../../tests/integration/duckdb/test_eligible_hash_stream.py)
- [`tests/integration/duckdb/test_preparation_recovery_encoding.py`](../../../tests/integration/duckdb/test_preparation_recovery_encoding.py)
- [`tests/integration/duckdb/test_prepared_encoding_guards.py`](../../../tests/integration/duckdb/test_prepared_encoding_guards.py)
- [`tests/integration/duckdb/test_resolution_parent_publication.py`](../../../tests/integration/duckdb/test_resolution_parent_publication.py)
- [`tests/integration/web/test_authoring_preparation_recovery.py`](../../../tests/integration/web/test_authoring_preparation_recovery.py)
- [`tests/integration/web/test_recipe_preparation_current_result.py`](../../../tests/integration/web/test_recipe_preparation_current_result.py)
- [`tests/application/workspace/preparation/test_recovery.py`](../../../tests/application/workspace/preparation/test_recovery.py)
- [`tests/application/run/test_stabilization.py`](../../../tests/application/run/test_stabilization.py)
- [`tests/domain/preparation/test_quality.py`](../../../tests/domain/preparation/test_quality.py)
- [`tests/domain/preparation/test_normalization.py`](../../../tests/domain/preparation/test_normalization.py)
- [`tests/application/workspace/preparation/test_normalization_metadata.py`](../../../tests/application/workspace/preparation/test_normalization_metadata.py)
- [`tests/application/workspace/preparation/test_normalization_timing.py`](../../../tests/application/workspace/preparation/test_normalization_timing.py)
- [`tests/integration/duckdb/test_normalization_seed_transport.py`](../../../tests/integration/duckdb/test_normalization_seed_transport.py)
- [`tests/performance/test_preparation_scale.py`](../../../tests/performance/test_preparation_scale.py)
- [`tests/performance/test_preparation_identity_groups.py`](../../../tests/performance/test_preparation_identity_groups.py)
- [`tests/integration/web/test_preparation_workflow.py`](../../../tests/integration/web/test_preparation_workflow.py)
- [`tests/integration/web/test_normalization_presenter.py`](../../../tests/integration/web/test_normalization_presenter.py)
- [`tests/application/workspace/test_workflow_issues.py`](../../../tests/application/workspace/test_workflow_issues.py)
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
