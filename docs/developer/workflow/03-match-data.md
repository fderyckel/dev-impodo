---
audience: developer
stage: match
status: current
---

# Match data

## Responsibility

Match data builds a portable mapping definition from frozen source datasets to
the governed Odoo schema. It owns recoverable drafts, immutable revisions,
semantic validation, exact submission, and an optional transformation-impact
preview with optional review decisions. It also projects one checked revision
and its validation result into a portable matching review workbook.

It does not prepare all rows, perform the final target comparison, or write to
Odoo.

## Entry conditions

A frozen source selection and captured Odoo schema are required. File-source
mappings also require current business-key governance. Pinned Odoo-source
mappings use protected capture identity and bind directly to the captured
schema hash. The editor binds every definition to the exact source-selection
and schema evidence.

## Implementation flow

`mapping.py` parses the browser form, requests bounded dynamic value choices,
saves drafts, promotes checked revisions, publishes transformation-impact
snapshots, records acknowledgements, and submits the exact valid revision.

`MappingWorkspaceService` separates a mutable working draft from immutable
revision, validation, and submission evidence. Domain validation checks scalar
providers, conversions, identities, relationships, write scope, and coverage.
`TransformationImpactService` evaluates the checked rules against frozen source
values without changing source evidence.

The browser default-recovery actions save the reviewed `odoo_default`
disposition before they validate the exact saved working draft. Individual,
grouped, and freshly captured default decisions each trigger one complete
mapping check. A valid result removes the resolved blocker. An invalid result
replaces the reason panel with only the current remaining blockers. The route
uses the submitted parent revision and the newly saved working-draft version as
concurrency guards. A grouped action performs one validation pass after all
decisions are saved; it does not validate once per field.

The `/mapping/review-workbook` routes create and download the workbook for the
exact current checked revision. `write_mapping_review_workbook` consumes only
the immutable revision, its bound validation result, the frozen source
selection, and the captured Odoo schema. It makes no Odoo call and does not
open a source or prepared-data artifact. An invalid validation result is a
supported input because the workbook is intended to help the operator correct
that result.

The workbook keeps validation severity authoritative. An error produces red
**Must fix** evidence, while a warning produces amber review evidence. A
confirmed Odoo-default disposition remains amber because the operator chose to
omit the value deliberately. A valid direct mapping is green, and a fixed or
transformed value is blue. Each table receives a column-based field view so a
required field remains visible even when it has no source provider. The
workbook also contains a filterable issue queue, a row-based field inventory,
bounded categorical coverage, and the validation contract's deferred checks.
It always pairs colour with a status and next action.

The stored filename binds the mapping revision version and content-hash prefix.
A checked revision can therefore use only its own workbook. A current saved
draft with different content blocks workbook creation and download until the
operator selects **Check matches** again. Protected Odoo-source business values
remain outside the portable value-coverage projection.

For each conditional Selection rule, the impact snapshot records every
evaluated row, every raw match before priority, every row selected by
first-match priority, and every row where that rule matched alongside another
rule. A zero-match fact and an overlap fact have separate stable fingerprints.
The data manager can inspect and acknowledge those facts, but the preview does
not gate mapping submission. Stage 4 prepares every row and owns the required
prepared-data review. The snapshot identity includes the mapping, source,
schema, evaluator, and impact-contract versions, so a rule edit or reorder
retires the prior optional decisions.

Optional Recipe publication compiles only an eligible submitted mapping and
its required portable contracts. It does not change this working draft or move
its source, schema, impact, preparation, comparison, or execution evidence.
Project-owned application workspaces and drift assessment belong to the
integrated Test workflow.

## Contract invariants

Each dataset declares one permitted target model and one operating mode:
`upsert`, `create`, `reference`, or `odoo_pinned_update`. Each scalar target
field has exactly one provider. The provider can use a source value, a
constant, a source value with a fallback, ordered conditional Selection rules,
or no sent value so Odoo can apply its default. Transformations, null behavior, comparison policy, and
relationship resolution use closed, versioned choices rather than arbitrary
code.

Mapping contract version 13 retains the `conditional_rules` scalar provider
introduced in version 12. A `SelectionRuleSet` preserves author order, applies
first-match-wins semantics, and ends with either one captured Odoo technical
choice or an explicit unresolved-row block. Each rule combines one to eight
typed source conditions with `all` or `any`; the complete field is bounded to
20 rules and 20 distinct source columns. The contract rejects a conditional
provider that also carries a source, literal, fallback, inline value match,
reference lookup, or hidden formula.

`evaluate_scalar_mapping_value` is the shared row oracle for preview and
preparation. `CategoricalCoverageService` projects the union of every
referenced source column in one dataset scan. The columnar compiler emits
`CONDITIONAL_SELECTION`, and the Polars adapter evaluates ordered branches as
native expressions without a Python UDF. None of these paths calls Odoo or
queries a repository inside a source-row loop.

The browser renders captured Odoo choices independently from source values.
The rule form stores strict JSON under 64 KiB, preserves stable rule and
condition UUIDs, and reuses the dataset's one lazy source-option template
instead of expanding every source column once per field or condition. Saving a
rule creates a new mapping hash, so existing validation, impact, submission,
preparation, comparison, and execution evidence no longer satisfies the
current mapping.

The current mapping contract records a required-field disposition when no
value is sent: `odoo_default` means the target configuration must supply the
value, while `odoo_managed` is limited to captured computed, related, one2many,
or many2many fields. Both remain warning-bearing decisions that require review;
Impodo does not call arbitrary Odoo `default_get` logic while editing a mapping.

The same contract provides `odoo_pinned_update` and a sorted
`approved_write_fields` set. A pinned mapping requires an `OdooSourceBinding`
and retains the originating model. It has no source or target business
identity, create policy, field disposition, or relationship write. Every
non-validation scalar mapping requires a separate approval.

Each approved write requires a captured baseline and fail-closed Tier-1 field
metadata. The field must use a supported scalar type and must be stored,
writable, non-computed, non-related, non-translated, and
non-company-dependent. The mapping content hash binds the approvals. Numeric
Odoo IDs never enter the portable mapping or canonical rows.

Many2one and many2many relationships use one of three closed origins.
`dataset` resolves through one incoming dataset, `target_catalog` resolves
through existing-target business keys, and `target_then_dataset` checks the
target before it falls back to the incoming dataset. A one2many relationship
is represented through the child dataset's inverse many2one field; Impodo
never writes it as an independently owned list. Dynamic value matching reads
one frozen source column and fetches target choices in batches. It persists
portable codes or business keys, never numeric Odoo IDs.

The `target_then_dataset` resolver retains both the reviewed Odoo key and the
original incoming key. Exact `ValueMapping` entries affect only the Odoo
lookup key. The source preparation path does not rewrite the incoming key, so
an unmatched value can still resolve to the intended incoming row. When the
target lookup returns one exact record, preflight gives that Odoo identity
precedence and classifies the corresponding incoming row as `UNCHANGED` with
no field differences. This precedence is a reference decision, not authority
to update the existing Odoo record.

String identity remains case-sensitive. The bounded record plan also captures
case-insensitive exact candidates with `=ilike`, but preflight uses those rows
only to emit `REFERENCE_CASE_MISMATCH_REVIEW_REQUIRED`. It never treats them as
equal. `TargetCatalog` caches exact and case-folded field indexes, and the read
planner batches distinct keys, so neither exact matching nor case review adds
one Odoo query or one target scan per source row.

Execution preserves the reviewed split. A `RESOLVED_TARGET` outcome becomes a
portable `BusinessReference`. A `RESOLVED_INCOMING` outcome becomes an
incoming `LogicalReference`, which keeps the dataset dependency needed to
create and link the missing related record.

Mapping contract version 13 optionally freezes a
`dataset_projection_field` on `target_then_dataset`. It is valid only when the
selected incoming dataset targets a different captured model and that model
exposes a read-only many2one to the relationship target model. Compilation
carries the field into the incoming `ResolveSpec`; execution snapshot version
7 then binds it to the exact relationship intent. Numeric generated IDs remain
runtime journal receipts and never enter the reusable mapping or prepared
canonical rows.

`extract_dataset_dependency_edges` gives browser mappings and compiled
profiles one incoming-dependency meaning. Target identity and scope edges are
hard. A relationship edge is hard when the compiled contract or captured Odoo
schema requires it during create; other incoming relationship edges are
deferrable. Validation retains self-references for row-level analysis and
rejects hard cycles that cross datasets. A required Odoo field is a target
constraint, so the browser form and final browser compiler preserve it even
when an older mapping did not set `required_on_create` explicitly.

The relationship validator and Recipe compiler share the reviewed Odoo 19
standard-reference registry. A resolver that exactly uses a registered key may
compile its narrow field contract without widening the primary schema scope.
The compiler rejects mixed, unregistered, writable, version-mismatched, or
metadata-mismatched reference use instead of guessing a contract.

Mapping validation contract version 3 binds the current reference-policy hash.
Supporting lookup contract version 2 binds the same hash to its lookup key and
snapshot. Retired evidence payloads are rejected rather than reused.

Cleanup is stored exclusively as ordered `text_steps`. Retired scalar
search/replacement fields are rejected rather than
silently converted or dropped. Quick matching remains bounded to 500 source
choices and 2,000 target records; composite or scoped identities use the normal
governed relationship workflow.

The recoverable working draft is deliberately non-authoritative. Semantic
validation creates immutable issues and coverage. Submission then binds the
exact valid revision to the source and schema evidence, semantic-validation
warning acknowledgement, and actor. The optional impact preview does not
authorize submission. Preparation and final review remain responsible for
row-level uniqueness and relationship resolution; a mapping preview does not
claim those results.

## Code references

| Role | Code |
| --- | --- |
| Mapping lifecycle | [`MappingWorkspaceService`](../../../src/impodo/application/workspace/mapping/service.py) |
| Mapping contracts | [`contracts.py`](../../../src/impodo/domain/mapping/contracts.py) |
| Semantic validator | [`validator.py`](../../../src/impodo/domain/mapping/validation/validator.py) |
| Governed-reference policy | [`reference_keys.py`](../../../src/impodo/domain/workspace/reference_keys.py) |
| Shared scalar and conditional-rule evaluator | [`scalar_values.py`](../../../src/impodo/domain/mapping/scalar_values.py) |
| Categorical source-domain scan | [`CategoricalCoverageService`](../../../src/impodo/application/workspace/mapping/categorical_coverage.py) |
| Native conditional-rule compiler | [`columnar_transformation.py`](../../../src/impodo/domain/compiler/columnar_transformation.py) |
| Rule-impact service | [`TransformationImpactService`](../../../src/impodo/application/workspace/mapping/transformation_impact.py) |
| Rule-impact facts and fingerprints | [`transformation_impact.py`](../../../src/impodo/domain/staging/transformation_impact.py) |
| Native rule-impact summary | [`polars_transformation.py`](../../../src/impodo/adapters/polars_transformation.py) |
| Rule-impact persistence and acknowledgements | [`TransformationImpactRepository`](../../../src/impodo/adapters/duckdb/transformation_impact_repository.py) |
| Matching review workbook | [`mapping_review.py`](../../../src/impodo/adapters/artifacts/mapping_review.py) |
| Optional Recipe compilation | [`RecipeCompiler`](../../../src/impodo/application/recipe_compilation_service.py) |
| Browser routes | [`mapping.py`](../../../src/impodo/web/routers/mapping.py) |
| Browser-to-runtime mapping compiler | [`browser_mapping_compiler.py`](../../../src/impodo/domain/compiler/browser_mapping_compiler.py) |
| Canonical relationship dependencies | [`relationship_dependencies.py`](../../../src/impodo/domain/relationship_dependencies.py) |
| Batched Odoo read planning | [`planner.py`](../../../src/impodo/domain/execution/planner.py) |
| Target-first resolution and classification | [`preflight.py`](../../../src/impodo/domain/preparation/preflight.py) |
| Reviewed execution hand-off | [`execution_snapshot.py`](../../../src/impodo/domain/execution_snapshot.py) |

## Evidence and state

The working draft is recoverable but non-authoritative. `MappingRevision`
stores immutable portable meaning. `MappingValidationResult` binds validation
to the revision. `MappingSubmission` binds the current actor decision to the
exact mapping content, source selection, schema, and semantic warning review.
`TransformationImpactSnapshot` remains separate, optional, read-only evidence.
The matching review workbook is also a projection rather than a new decision
source. It cannot change the validation status, acknowledge a warning, confirm
the mapping, or qualify prepared data. The Stage 5 workbook remains a separate
artifact derived from prepared rows and fresh target-comparison evidence.

## Completion and navigation

Navigation marks Match data complete only when the current revision has a
submission with the same mapping ID and content hash. A draft or validation
without matching submission does not unlock Prepare data.

## Invalidation and recovery

Source or schema changes invalidate the current mapping boundary. Editing a
submitted mapping creates new work; it never rewrites the old revision.
Configured text steps and conditional Selection rules can produce current
optional effect evidence. A cleanup step with no effect or a Selection rule
with zero matches or overlapping priority remains visible in that preview, but
it does not block submission. Stage 4 still requires current prepared evidence
and review before the workflow can continue.

Form parsers must reject unexpected fields and stale versions. Preserve the
working draft when validation fails so the data manager can correct it.
Whenever confirmation is unavailable, the page must show every current blocker
outside paged or filtered field lists and link directly to a recovery action.
The same checked blockers remain exportable through the matching review
workbook. Saving any different working draft makes the prior download
ineligible even when its workbook file still exists as historical evidence.

## Odoo 19 and performance

Use Odoo technical field names and stable selection codes internally while
presenting business labels. Relationship validation must resolve by portable
identity and batch catalogue reads; never search Odoo once per mapping row or
source value.

Transformation impact must remain bounded and hash-bound. Reusing an impact
report after a mapping edit would be a correctness defect even if its counts
look plausible.

Workbook generation is bounded by the captured schema, validation issues,
categorical coverage, and Excel's row and column limits. It must not scan source
rows, reopen repositories for individual fields, or contact Odoo. Workbook
cells must neutralize spreadsheet formulas in external text. Creation requires
protected-evidence management authority, while download requires
protected-evidence read authority.

## Verification

- [`tests/integration/web/test_mapping_forms.py`](../../../tests/integration/web/test_mapping_forms.py)
- [`tests/domain/mapping/test_validation.py`](../../../tests/domain/mapping/test_validation.py)
- [`tests/domain/mapping/test_selection_rules.py`](../../../tests/domain/mapping/test_selection_rules.py)
- [`tests/integration/web/test_mapping_impact_presenter.py`](../../../tests/integration/web/test_mapping_impact_presenter.py)
- [`tests/integration/web/test_mapping_workflow.py`](../../../tests/integration/web/test_mapping_workflow.py)
- [`tests/integration/artifacts/test_mapping_review_workbook.py`](../../../tests/integration/artifacts/test_mapping_review_workbook.py)
- [`tests/domain/recipe/test_representative_shapes.py`](../../../tests/domain/recipe/test_representative_shapes.py)
- [`tests/domain/preparation/test_target_first_relationships.py`](../../../tests/domain/preparation/test_target_first_relationships.py)
- [`tests/domain/test_relationship_dependencies.py`](../../../tests/domain/test_relationship_dependencies.py)

Verify draft recovery, stale versions, semantic validation, relation modes,
ordered transformations, optional zero-match and overlap review, hash binding,
direct exact submission, target-first reuse without updates, case-sensitive
relationship matching, incoming fallback, and required Stage 4 review.
For the workbook, also verify invalid-check export, issue precedence, written
status alongside colour, exact-revision download, Odoo-source value redaction,
and separation from the Stage 5 workbook.

Run the focused Mapping package with:

```bash
.venv/bin/python -m unittest \
  tests.integration.web.test_mapping_forms \
  tests.domain.mapping.test_validation \
  tests.domain.mapping.test_selection_rules \
  tests.integration.web.test_mapping_impact_presenter -v
```

## Related documentation

- [User guide: Match data](../../user/workflow/03-match-data.md)
- [Workflow evidence lifecycle](../contracts/evidence-lifecycle.md)
- [Canonical staging contract](../contracts/canonical-staging.md)
