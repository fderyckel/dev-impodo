---
audience: developer
kind: contract
status: current
---

# Workflow evidence lifecycle contract

## Scope

This contract defines bindings and invalidation across Recipe publication and
application plus Source data, Odoo data, and Match data. Stage-specific routes,
services, formats, and tests belong in the corresponding developer workflow
pages.

Every dataset uses one discriminated source binding:

- `FILE` binds a confirmed registered file table and frozen source snapshot;
- `ODOO` binds an authenticated, bounded capture selection and published
  snapshot;
- `DERIVED` binds a versioned structural rule and its exact input datasets.

There are no placeholder files or alternate historical JSON shapes.

## Current evidence chains

The reusable lineage is:

```text
Recipe + current Authoring DataVersion workspace
-> read-only RecipeDraft projection
-> immutable portable RecipeRevision
-> fresh Test DataVersion + TargetBinding + RecipeApplicationEvidence
-> Test preparation, comparison, execution, read-back, and reconciliation
-> RecipeQualification
-> explicit CutoverCandidate
-> fresh Production DataVersion pinned to that exact revision
```

The contained workspace chain inside each DataVersion is:

```text
registered MigrationProject workspace
-> current source catalogue and confirmation
-> frozen source selection and snapshots
-> current target schema and governed business keys
-> mapping revision, validation, impact review, and submission
```

Each pointer selects one current immutable revision. Historical revisions
remain available for audit but do not satisfy current-stage prerequisites.
The two chains meet through exact Recipe, revision, DataVersion, workspace,
application, and evidence hashes; no stage infers linkage from display names.

## Binding rules

Source confirmation binds source and catalogue hashes, parsing/header settings,
the selected physical table, warning acknowledgement, actor, and timestamp.
Source freeze binds stable dataset and column identities, row counts, lineage,
reader version, logical content hash, and immutable snapshot location.

Live target schema binds target identity, permitted models, effective fields,
relation and selection metadata, and read-credential provenance. A local manual
draft is unverified and cannot authorize mapping submission.

Schema governance binds confirmed natural business keys and optional scope to
one exact schema revision. Mapping binds the exact source selection, schema,
governance, providers, transformations, relationships, validation result,
reviewed warnings, and actor submission.

Mapping contract v11 additionally binds an explicit closed-domain policy for
every scalar selection and relationship. Application validation scans each
affected physical dataset once across all relevant fields and embeds immutable
`CategoricalCoverageEvidence` in validation contract v2, so its content hash
participates in the submission validation hash. Relationship target existence
and uniqueness remain declared deferred checks satisfied by fresh preparation
evidence; mapping validation does not claim target-record coverage. Reusable
control definitions and the current DataVersion's expected values are separate
v11 objects, projected back to effective totals only at preparation time.
Recipe applications persist current `RecipeControlValues` and
`RecipeParameterValues` as hash-pinned DataVersion evidence; they are not part
of reusable Recipe semantic identity.

A Recipe revision contains logical source, preparation, mapping, target,
quality, reference, parameter-definition, and control-definition meaning. It
does not contain physical source IDs or rows, a concrete target, credentials,
approvals, comparison output, execution journals, or reconciliation results.
Application of that revision creates fresh workspace evidence and a normal
mapping draft; it never copies a prior workspace database.

Portable evidence uses business keys and stable technical names. Numeric Odoo
record IDs may appear only in protected target-specific evidence and never as
portable source or relationship identities.

## Invalidation matrix

| Change | Current evidence invalidated |
| --- | --- |
| Publish a new Recipe revision | The new revision is untested; prior qualification remains history and is not transferred |
| Start a successor DataVersion | The predecessor workspace is sealed; the new workspace begins without operational evidence |
| Change current DataVersion parameters or controls | Current application review/evidence and dependent workspace evidence |
| Change source/target structure or credential generation during application | Current application or TargetBinding; the immutable Recipe revision remains unchanged |
| Reinspect or reconfirm a file | Frozen source selection, snapshots, derived plans, mapping, and downstream evidence |
| Freeze a new source selection | Derived plans, mapping, and downstream evidence |
| Change an Odoo capture plan | Prior current Odoo snapshot, mapping, and downstream evidence |
| Recapture target schema | Schema governance, mapping, and downstream evidence |
| Change target identity or model scope | Target schema, governance, mapping, comparison, and execution evidence |
| Change governed business keys | Mapping, target comparison, and execution evidence |
| Save or remove a related-dataset plan | Mapping and downstream prepared evidence |
| Save a new mapping revision | Prior validation, impact review, submission, and downstream evidence |

Invalidation retires current pointers; it does not rewrite or delete historical
evidence. Regeneration starts at the earliest changed stage.

## Draft recovery

A recoverable mapping draft may survive an upstream change, but it can be
restored only when its source-selection and governed-schema bindings still
match. Stale working state must be disclosed and must never be applied to a
different dataset or field catalogue.

## Access and performance boundaries

Source inspection, mapping preview, and preparation use bounded local evidence.
Target metadata and record reads are planned and batched by model. No connector
call, metadata lookup, or database query is permitted inside a source-row loop.

## Related documentation

- [Recipe and data-version lifecycle](recipe-lifecycle.md)
- [Source data](../workflow/01-source-data.md)
- [Odoo data](../workflow/02-odoo-data.md)
- [Match data](../workflow/03-match-data.md)
