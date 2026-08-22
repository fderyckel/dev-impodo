---
audience: developer
stage: match
status: current
---

# Match data

## Responsibility

Match data builds a portable mapping definition from frozen source datasets to
the governed Odoo schema. It owns recoverable drafts, immutable revisions,
semantic validation, transformation-impact evidence, warning acknowledgement,
and exact submission.

It does not prepare all rows or write to Odoo.

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

For each conditional Selection rule, the impact snapshot records every
evaluated row, every raw match before priority, every row selected by
first-match priority, and every row where that rule matched alongside another
rule. A zero-match fact and an overlap fact have separate stable fingerprints.
The data manager must acknowledge every current warning fact or edit the rule
before submission. The snapshot identity includes the mapping, source,
schema, evaluator, and impact-contract versions, so a rule edit or reorder
retires the prior decisions.

For a published Recipe Test or Production application,
`RecipeApplicationService.apply` first checks exact current source, target,
parameter, control, reference, credential-generation, and categorical
evidence. A compatible application saves an ordinary fresh
`MappingWorkingDraft`; it does not bypass this screen's validation, impact,
acknowledgement, or submission flow. The presenter adds only a status banner
identifying the Recipe-built draft.

Test applies the current published revision. Production applies the exact
qualified cutover-candidate revision, which may intentionally be older than
the Recipe's current revision. Neither application can publish new Recipe
meaning from its contained workspace.

## Contract invariants

Each dataset declares one permitted target model and one operating mode:
`upsert`, `create`, `reference`, or `odoo_pinned_update`. Each scalar target
field has exactly one provider. The provider can use a source value, a
constant, a source value with a fallback, ordered conditional Selection rules,
or no sent value so Odoo can apply its default. Transformations, null behavior, comparison policy, and
relationship resolution use closed, versioned choices rather than arbitrary
code.

Mapping contract version 12 adds `conditional_rules` as a first-class scalar
provider. A `SelectionRuleSet` preserves author order, applies first-match-wins
semantics, and ends with either one captured Odoo technical choice or an
explicit unresolved-row block. Each rule combines one to eight typed source
conditions with `all` or `any`; the complete field is bounded to 20 rules and
20 distinct source columns. The contract rejects a conditional provider that
also carries a source, literal, fallback, inline value match, reference lookup,
or hidden formula.

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

Mapping contract version 9 also records a required-field disposition when no
value is sent: `odoo_default` means the target configuration must supply the
value, while `odoo_managed` is limited to captured computed, related, one2many,
or many2many fields. Both remain warning-bearing decisions that require review;
Impodo does not call arbitrary Odoo `default_get` logic while editing a mapping.
Version 8 mappings remain readable and retain their original content hashes.

Mapping contract version 10 adds `odoo_pinned_update` and a sorted
`approved_write_fields` set. A pinned mapping requires an `OdooSourceBinding`
and retains the originating model. It has no source or target business
identity, create policy, field disposition, or relationship write. Every
non-validation scalar mapping requires a separate approval.

Each approved write requires a captured baseline and fail-closed Tier-1 field
metadata. The field must use a supported scalar type and must be stored,
writable, non-computed, non-related, non-translated, and
non-company-dependent. The mapping content hash binds the approvals. Numeric
Odoo IDs never enter the portable mapping or canonical rows.

Many2one and many2many relationships resolve through incoming datasets or
existing-target business keys. A one2many relationship is represented through
the child dataset's inverse many2one field; Impodo never writes it as an
independently owned list. Dynamic value matching reads one frozen source column
and fetches target choices in batches. It persists portable codes or business
keys, never numeric Odoo IDs.

The relationship validator and Recipe compiler share the reviewed Odoo 19
standard-reference registry. A resolver that exactly uses a registered key may
compile its narrow field contract without widening the primary schema scope.
The compiler rejects mixed, unregistered, writable, version-mismatched, or
metadata-mismatched reference use instead of guessing a contract.

Mapping validation contract version 3 binds the current reference-policy hash.
Supporting lookup contract version 2 binds the same hash to its lookup key and
snapshot. Older evidence remains readable for audit, but it cannot authorize a
new submission or be reused as a current lookup after the policy changes.

Since mapping contract version 8, cleanup is stored exclusively as ordered
`text_steps`. Legacy scalar search/replacement fields are rejected rather than
silently converted or dropped. Quick matching remains bounded to 500 source
choices and 2,000 target records; composite or scoped identities use the normal
governed relationship workflow.

The recoverable working draft is deliberately non-authoritative. Semantic
validation creates immutable issues and coverage. Submission then binds the
exact valid revision to the source and schema evidence, impact review, warning
acknowledgement, and actor. Preparation and final review remain responsible for
row-level uniqueness and relationship resolution; a mapping preview does not
claim those results.

## Code references

| Role | Code |
| --- | --- |
| Mapping lifecycle | [`MappingWorkspaceService`](../../../src/impodo/application/mapping_workspace_service.py) |
| Mapping contracts | [`contracts.py`](../../../src/impodo/domain/mapping/contracts.py) |
| Semantic validator | [`validator.py`](../../../src/impodo/domain/mapping/validation/validator.py) |
| Governed-reference policy | [`reference_keys.py`](../../../src/impodo/reference_keys.py) |
| Shared scalar and conditional-rule evaluator | [`scalar_values.py`](../../../src/impodo/domain/mapping/scalar_values.py) |
| Categorical source-domain scan | [`CategoricalCoverageService`](../../../src/impodo/application/categorical_coverage_service.py) |
| Native conditional-rule compiler | [`columnar_transformation.py`](../../../src/impodo/domain/compiler/columnar_transformation.py) |
| Rule-impact service | [`TransformationImpactService`](../../../src/impodo/application/transformation_impact_service.py) |
| Rule-impact facts and fingerprints | [`transformation_impact.py`](../../../src/impodo/domain/staging/transformation_impact.py) |
| Native rule-impact summary | [`polars_transformation.py`](../../../src/impodo/adapters/polars_transformation.py) |
| Rule-impact persistence and acknowledgements | [`TransformationImpactRepository`](../../../src/impodo/adapters/duckdb/transformation_impact_repository.py) |
| Recipe draft compilation | [`RecipeApplicationService`](../../../src/impodo/application/recipe_application_service.py) |
| Browser routes | [`mapping.py`](../../../src/impodo/web/routers/mapping.py) |

## Evidence and state

The working draft is recoverable but non-authoritative. `MappingRevision`
stores immutable portable meaning. `MappingValidationResult` binds validation
to the revision. `MappingSubmission` binds the current actor decision to the
exact mapping content, source selection, schema, warning review, and impact
evidence.

## Completion and navigation

Navigation marks Match data complete only when the current revision has a
submission with the same mapping ID and content hash. A draft or validation
without matching submission does not unlock Prepare data.

## Invalidation and recovery

Source or schema changes invalidate the current mapping boundary. Editing a
submitted mapping creates new work; it never rewrites the old revision.
Configured text steps and conditional Selection rules require current effect
evidence. A cleanup step with no effect and a Selection rule with no matches
must be changed or acknowledged explicitly before submission. A Selection
rule that overlaps another rule also requires an explicit acknowledgement of
the current first-match priority.

Form parsers must reject unexpected fields and stale versions. Preserve the
working draft when validation fails so the data manager can correct it.
Whenever confirmation is unavailable, the page must show every current blocker
outside paged or filtered field lists and link directly to a recovery action.

## Odoo 19 and performance

Use Odoo technical field names and stable selection codes internally while
presenting business labels. Relationship validation must resolve by portable
identity and batch catalogue reads; never search Odoo once per mapping row or
source value.

Transformation impact must remain bounded and hash-bound. Reusing an impact
report after a mapping edit would be a correctness defect even if its counts
look plausible.

## Verification

- [`tests/test_mapping_forms.py`](../../../tests/test_mapping_forms.py)
- [`tests/test_mapping_validation.py`](../../../tests/test_mapping_validation.py)
- [`tests/test_selection_rules.py`](../../../tests/test_selection_rules.py)
- [`tests/test_mapping_impact_presenter.py`](../../../tests/test_mapping_impact_presenter.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)
- [`tests/test_recipe_application.py`](../../../tests/test_recipe_application.py)

Verify draft recovery, stale versions, semantic validation, relation modes,
ordered transformations, zero-match and overlap acknowledgement, hash binding,
and exact submission.

## Related documentation

- [User guide: Match data](../../user/workflow/03-match-data.md)
- [Workflow evidence lifecycle](../contracts/evidence-lifecycle.md)
- [Canonical staging contract](../contracts/canonical-staging.md)
