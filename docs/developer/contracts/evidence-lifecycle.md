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

When an Odoo source includes several selected models, each dataset keeps its
own `ODOO` binding, selection, snapshot, and protected origin sidecar. One
`SourceSelection` binds the complete dataset set. Current selection pointers
are keyed by model, current manifest pointers are keyed by dataset, and the
complete set advances atomically.

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

Integrated Test application then creates a separate current branch for each
selected revision:

```text
accepted Test DataVersion + exact Recipe revisions
-> one MigrationRun with shared target and requirement evidence
-> isolated RecipeApplications and MigrationWorkspaces
-> fresh mapping and focused current-data issues
```

The contained mapping-engine chain is:

```text
READY `MigrationWorkspace`
-> DataVersion-owned source catalogue and confirmation
-> frozen source selection and snapshots
-> current target schema and governed business keys
-> mapping revision, validation, and submission
```

The checked mapping can also produce an optional, read-only
transformation-impact snapshot. That preview does not authorize submission or
replace the required prepared-data review in Stage 4.

The engine's flat `WorkspaceState` object is a workbench projection over these
owners. It is not another identity or lifecycle. Project fields come from
`MigrationProject`; source fields come from the DataVersion package; mutable
target setup and immutable target binding come from the MigrationRun.

Each pointer selects one current immutable revision. Historical revisions
remain available for audit but do not satisfy current-stage prerequisites.
The chains meet through exact Project, DataVersion, workspace, mapping, and
Recipe revision hashes; no stage infers linkage from display names. Recipe
publication does not move or copy the DataVersion evidence into the Recipe.

Project-owned multi-Recipe application planning, qualification, CutoverPlans,
and Production orchestration are current. None can be inferred from an
application's setup state alone.

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

Read access evidence identifies the authenticated principal, readable models,
and effective available company IDs. Its company-scope fingerprint excludes
language, timezone, archived-record visibility, and other read-operation flags;
those settings describe the governed read, not a change in company access.

A later target-schema check first compares a validated candidate with the
current semantic evidence. Capture/check times, actors, credential generations,
and translated labels do not change that semantic identity. An unchanged check
retains the current schema hash and dependent pointers. A detected change keeps
the candidate unconfirmed beside the current catalogue. Only explicit
confirmation publishes the candidate and crosses the invalidation boundary.

Schema governance binds confirmed natural business keys and optional scope to
one exact schema revision. Mapping evidence then binds the exact source
selection, schema, governance, providers, transformations, relationships,
validation result, reviewed warnings, and actor submission.

A mapping mutation receipt is separate operational recovery evidence. Its UUID
binds one actor and exact submitted command meaning. The committed receipt is
written in the same workspace-engine transaction as the authoritative mapping
draft, revision, validation, or submission change and records the resulting
versions and content identity. A rejected or pending receipt never validates,
submits, or otherwise authorizes mapping evidence. Pending means the outcome is
still unknown and forbids automatic mutation replay.

The exact current mapping contract is version 15. It binds an explicit
closed-domain policy for every scalar selection and relationship. Application
validation scans each affected physical dataset once across all relevant
fields and embeds immutable `CategoricalCoverageEvidence` in validation
contract version 3, so its content hash participates in the submission
validation hash. Relationship target existence and uniqueness remain declared
deferred checks satisfied by fresh preparation evidence; mapping validation
does not claim target-record coverage. Reusable control definitions and the
current DataVersion's expected values are separate objects, projected to
effective totals only at preparation time. Current Project work keeps expected
values and parameter choices as DataVersion or workspace evidence; they are
not reusable Recipe identity unless the Recipe contract explicitly defines
their portable shape.

Mapping contract version 13 added the optional captured projection used when
Odoo creates a relationship target from an imported source record. Version 14
added ordered two-to-five-column text concatenation. Version 15 adds a closed
relationship value provider and a portable constant business reference for a
many2one that uses the same existing Odoo record for every row. The constant
stores ordered governed key and scope values without a source-column binding
or numeric Odoo ID. Version-aware decoding keeps earlier field layouts closed;
v14 relationships decode as source-provided relationships.

The contract retains the conditional Selection providers introduced in
version 12. It binds ordered rule and condition identifiers, typed
comparisons, referenced source-column keys, captured Odoo output keys, and the
otherwise decision. Categorical validation
projects all referenced columns in the dataset's existing bounded scan and
fails closed when a row cannot produce a current Odoo choice. Recipe
publication replaces physical column keys with logical source-column IDs;
application rebinds those IDs to the fresh frozen selection and revalidates
the current Odoo choices before creating a normal mapping draft. A constant
existing relationship has no source-column ID to bind; application instead
rechecks its portable business reference against the fresh target evidence.

Governed-reference policy version 1 has one canonical hash shared by Match,
supporting lookups, Final review, and optional Recipe publication.
Mapping validation contract version 3, supporting lookup contract version 2,
preflight requirement-plan contract version 2, and Recipe target-contract
version 2 bind that hash. Retired payload versions are rejected rather than
loaded, upgraded, or silently reused.

### Portable matching review

Stage 3 may project an exact mapping revision and its bound validation result
into a portable matching review workbook. The projection is available for
`VALID`, `VALID_WITH_WARNINGS`, and `INVALID` results because it helps the data
manager correct known mapping problems. The validation result remains
authoritative: errors are **Must fix**, warnings require review, and workbook
appearance cannot introduce or remove a validation finding.

The workbook may add captured source-table, Odoo-model, and Odoo-field labels.
It may also add the bounded categorical counts and uncovered values already
contained in the exact validation evidence. Protected Odoo-source business
values must remain inside Impodo. External workbook text must not become an
executable spreadsheet formula.

The workbook does not contain prepared rows, duplicate decisions, final
relationship outcomes, fresh target records, create/update classifications, or
field differences against Odoo. It lists deferred runtime checks rather than
claiming their results. It cannot confirm a mapping, acknowledge a warning,
qualify preparation, or authorize execution. A changed working draft, source
selection, governed schema, or validation binding makes the prior workbook
ineligible for current download.

The transformation-impact snapshot records two hash-bound facts for each
conditional Selection rule. The match fact counts rows that matched before
priority and rows that the rule selected after first-match priority. The
overlap fact counts rows where that rule matched alongside another rule. A
zero-match fact or nonzero overlap fact remains available for optional review;
neither fact blocks mapping submission. Changing or reordering a rule changes
the mapping and impact identities, so an older preview or acknowledgement
cannot describe the new revision.

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
| Change one Odoo capture plan | The complete current Odoo snapshot set, mapping, and downstream evidence |
| Check target schema and find no semantic change | No invalidation; update freshness and access provenance only |
| Detect a target-schema change | No invalidation; preserve current evidence, mark Odoo data **Needs attention**, and block a new Odoo source freeze |
| Confirm a detected target-schema change | Schema governance, Odoo capture selection and snapshot pointers, mapping, and downstream evidence |
| Change target identity or model scope | Target schema, governance, mapping, comparison, and execution evidence |
| Change governed business keys | Mapping, target comparison, and execution evidence |
| Change the governed-reference policy | Mapping validation and submission, supporting lookups, preparation, comparison, and new Recipe target contracts |
| Save or remove a related-dataset plan | Mapping and downstream prepared evidence |
| Save a new mapping revision | Prior validation, impact review, submission, and downstream evidence |
| Change a constant relationship provider, key, scope value, required policy, or failure policy | Prior validation, impact review, submission, preparation, comparison, transfer-order, execution, and reconciliation evidence |
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
- [Integrated Test run lifecycle](integrated-run-lifecycle.md)
- [Source data](../workflow/01-source-data.md)
- [Odoo data](../workflow/02-odoo-data.md)
- [Match data](../workflow/03-match-data.md)
