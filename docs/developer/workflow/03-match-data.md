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

A frozen source selection, captured Odoo schema, and current business-key
governance are required. The editor must bind its definition to the exact
source-selection and schema hashes.

## Implementation flow

`mapping.py` parses the browser form, requests bounded dynamic value choices,
saves drafts, promotes checked revisions, publishes transformation-impact
snapshots, records acknowledgements, and submits the exact valid revision.

`MappingWorkspaceService` separates a mutable working draft from immutable
revision, validation, and submission evidence. Domain validation checks scalar
providers, conversions, identities, relationships, write scope, and coverage.
`TransformationImpactService` evaluates the checked rules against frozen source
values without changing source evidence.

## Code references

| Role | Code |
| --- | --- |
| Mapping lifecycle | [`MappingWorkspaceService`](../../../src/impodo/application/mapping_workspace_service.py) |
| Mapping contracts | [`contracts.py`](../../../src/impodo/domain/mapping/contracts.py) |
| Semantic validator | [`validator.py`](../../../src/impodo/domain/mapping/validation/validator.py) |
| Rule-impact service | [`TransformationImpactService`](../../../src/impodo/application/transformation_impact_service.py) |
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
Configured text steps require current effect evidence. A zero-match rule must
be changed or acknowledged explicitly before submission.

Form parsers must reject unexpected fields and stale versions. Preserve the
working draft when validation fails so the data manager can correct it.

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
- [`tests/test_mapping_impact_presenter.py`](../../../tests/test_mapping_impact_presenter.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)

Verify draft recovery, stale versions, semantic validation, relation modes,
ordered transformations, zero-match acknowledgement, hash binding, and exact
submission.

## Related documentation

- [User guide: Match data](../../user/workflow/03-match-data.md)
- [Browser workspace contract](../../contracts/02-workspace.md)
- [Canonical staging contract](../../contracts/03-canonical-staging.md)
