---
audience: developer
kind: contract
status: current
---

# Workflow evidence lifecycle contract

## Scope

This contract defines how Project-owned evidence binds Source data, Odoo data,
and Match data, and how optional Recipe publication reads that evidence without
taking ownership of it. It also defines when a change invalidates the evidence.
The corresponding developer workflow pages own stage-specific routes,
services, formats, and tests.

Every dataset uses one discriminated source binding:

- A `FILE` binding identifies one confirmed registered file table and its
  frozen source snapshot.
- An `ODOO` binding identifies one authenticated, bounded capture selection
  and its published snapshot.
- A `DERIVED` binding identifies one versioned structural rule and its exact
  input datasets.

There are no placeholder files or alternate historical JSON shapes.

## Current evidence chains

The implemented Project lineage is:

```text
MigrationProject
-> Authoring DataVersion with one complete frozen source package
-> Authoring MigrationRun
-> MigrationWorkspace with selected dataset and snapshot references
-> mapping, preparation, comparison, execution, and reconciliation evidence
```

Optional reusable publication branches from the eligible workspace:

```text
current immutable workspace evidence
-> portable RecipeDefinition compilation
-> immutable Project-scoped RecipeRevision
```

The contained mapping-engine chain is:

```text
registered `WorkspaceState`
-> current source catalogue and confirmation
-> frozen source selection and snapshots
-> current target schema and governed business keys
-> mapping revision, validation, impact review, and submission
```

Each pointer selects one current immutable revision. Historical revisions
remain available for audit but do not satisfy current-stage prerequisites.
The chains meet through exact Project, DataVersion, workspace, mapping, and
Recipe revision hashes; no stage infers linkage from display names. Recipe
publication does not move or copy the DataVersion evidence into the Recipe.

Project-owned multi-Recipe applications, qualification, and CutoverPlans belong
to Phase M4 and later. They are not an active evidence chain in M3.

## Binding rules

Source confirmation binds the source and catalogue hashes to the parsing and
header settings, selected physical table, warning acknowledgement, actor, and
timestamp. Source freeze then binds stable dataset and column identities, row
counts, lineage, reader version, logical content hash, and immutable snapshot
location.

Live target schema evidence binds the target identity to the permitted models,
effective fields, relationship and selection metadata, and read-credential
provenance. A local manual draft remains unverified and cannot authorize
mapping submission.

Schema governance binds confirmed natural business keys and optional scope to
one exact schema revision. Mapping evidence then binds the exact source
selection, schema, governance, providers, transformations, relationships,
validation result, reviewed warnings, and actor submission.

Mapping contract v11 additionally binds an explicit closed-domain policy for
every scalar selection and relationship. Application validation scans each
affected physical dataset once across all relevant fields and embeds immutable
`CategoricalCoverageEvidence` in validation contract v2, so its content hash
participates in the submission validation hash. Relationship target existence
and uniqueness remain declared deferred checks satisfied by fresh preparation
evidence; mapping validation does not claim target-record coverage. Reusable
control definitions and the current DataVersion's expected values are separate
v11 objects, projected back to effective totals only at preparation time.
Current Project work keeps expected values and parameter choices as
DataVersion/workspace evidence; they are not part of reusable Recipe semantic
identity unless the Recipe contract explicitly defines their portable shape.

Mapping contract v12 binds conditional Selection providers, ordered rule and
condition identifiers, typed comparisons, referenced source-column keys,
captured Odoo output keys, and the otherwise decision. Categorical validation
projects all referenced columns in the dataset's existing bounded scan and
fails closed when a row cannot produce a current Odoo choice. Recipe
publication replaces physical column keys with logical source-column IDs;
application rebinds those IDs to the fresh frozen selection and revalidates
the current Odoo choices before creating a normal mapping draft.

Governed-reference policy version 1 has one canonical hash shared by Match,
supporting lookups, Final review, and optional Recipe publication.
Mapping validation contract version 3, supporting lookup contract version 2,
preflight requirement-plan contract version 2, and Recipe target-contract
version 2 bind that hash. Older versions remain readable for audit, but they
cannot silently become current evidence under a changed policy.

The transformation-impact snapshot records two hash-bound facts for each
conditional Selection rule. The match fact counts rows that matched before
priority and rows that the rule selected after first-match priority. The
overlap fact counts rows where that rule matched alongside another rule. A
zero-match fact or nonzero overlap fact blocks mapping submission until the
data manager edits the rule or acknowledges that exact current fingerprint.
Changing or reordering a rule changes the mapping and impact identities, so
the previous acknowledgement cannot satisfy the new revision.

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
| Change the governed-reference policy | Mapping validation and submission, supporting lookups, preparation, comparison, and new Recipe target contracts |
| Save or remove a related-dataset plan | Mapping and downstream prepared evidence |
| Save a new mapping revision | Prior validation, impact review, submission, and downstream evidence |
| Add, remove, edit, or reorder a conditional Selection rule | Prior categorical coverage, impact review, submission, preparation, comparison, and execution evidence |

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
