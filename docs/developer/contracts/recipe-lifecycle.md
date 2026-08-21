---
audience: developer
kind: contract
status: current
---

# Recipe and data-version lifecycle contract

## Scope

The browser calls the reusable migration effort a **project** because that is
the operator's business concept. In code and durable storage its aggregate
root is `Recipe`. Do not use `MigrationProject` as a synonym for either term.

### Vocabulary

| Term | Contract meaning |
| --- | --- |
| Operator project | Browser label for one reusable migration effort; it resolves to one `Recipe` |
| `Recipe` | Reusable business identity, lineage owner, and optimistic aggregate root |
| `RecipeRevision` | Immutable, portable version of reusable migration meaning |
| `DataVersion` | One exact Authoring, Test, or Production data package and lifecycle context |
| `MigrationProject` | Internal contained workspace that stores one DataVersion's source, target, mapping, evidence, credentials, and audit state |
| `TargetBinding` | Non-secret proof that one Recipe application was assessed against one exact current Odoo target |
| `RecipeQualification` | Immutable proof that one exact Recipe revision completed the required Test workflow and expected outcomes |
| `CutoverCandidate` | Explicit pointer to one exact current qualification; it grants no Production write authority |

Recipe, DataVersion, and workspace project IDs are independent UUID namespaces.
Resolution must reject an ID supplied in the wrong namespace.

## Lifecycle

**New project** creates the Recipe, Authoring DataVersion 1, and its contained
workspace as one recoverable operation. The Authoring DataVersion is not pinned
to a Recipe revision because it is where the next revision is compiled.

A Test DataVersion pins the Recipe's current published immutable revision. A
Production DataVersion requires a selected cutover candidate and pins that
candidate's exact qualified revision. It must not substitute a newer current
revision. Activating a successor DataVersion seals its predecessor in both the
registry and local workspace; sealed workspaces reject mutation.

Test and Production workspaces apply reusable meaning but cannot publish new
Recipe meaning. Any semantic correction starts in an Authoring workspace and
produces a new immutable Recipe revision.

## Publication boundary

`RecipeDraft` is a read-only projection over current Authoring evidence. It
does not copy or become an alternative mapping, quality, source, schema, or
preparation draft. Publication requires current, compatible source, submitted
mapping, schema governance, quality, and any declared reference, preparation,
parameter, or control evidence.

Reusable Recipe meaning may contain:

- logical source tables and columns;
- preparation, mapping, transformation, relationship, and disposition rules;
- Odoo model/field requirements and governed business keys;
- reusable manager-authored quality rules and reference dependencies; and
- parameter definitions and reusable control definitions.

It must exclude:

- source rows, source-file IDs, physical dataset/column IDs, and current source
  hashes;
- endpoint, database, target, schema-capture, principal, permission, company,
  credential, actor, and timestamp identity from semantic meaning;
- numeric Odoo record IDs and target snapshots; and
- approvals, execution journals, read-back, and reconciliation evidence.

Physical Authoring hashes, actor, and time may appear only in protected
publication provenance. Publishing semantic meaning that already exists in the
same Recipe is rejected rather than creating a duplicate revision.

## Application and drift boundary

Test and Production application create clean file-source workspaces. They do
not clone a previous DuckDB database or copy source, target, mapping,
preparation, quality, comparison, approval, credential, execution, or
reconciliation evidence.

Application binds required source tables by logical name and used columns by
source name. Reordered columns are compatible. New unused tables or columns are
informational. A renamed used column requires an explicit DataVersion-only
physical override. Missing structure, stale overrides, undeclared or missing
parameters/controls, uncovered categorical values, incompatible Odoo fields,
missing references, and stale credential generations block only the current
application; they do not mutate the published Recipe.

An accepted application creates a fresh ordinary `MappingWorkingDraft`,
rebuilds supported preparation and governance, and stages reusable quality
rules against the fresh mapping hash. The existing Match, Prepare, Final
review, Load, and reconciliation services remain authoritative; there is no
parallel Recipe execution engine.

## Qualification and rollout boundary

Qualification is available only from the current Recipe revision's exact
remote Test application after preparation, quality, comparison, execution,
read-back, and reconciliation succeed. The data manager must confirm the exact
expected create, update, unchanged, and verified totals.

Qualification and cutover selection are separate actions. Publishing a later
revision makes that revision untested and does not transfer an earlier
qualification. A previously selected candidate continues to identify its exact
older revision until explicitly replaced.

**Run with latest data** creates a fresh Production DataVersion from the
selected candidate. Production source, target, read credential, comparison,
approval, and write credential evidence are established again. The
`PRODUCTION` purpose describes Recipe lineage and target intent; it does not
override the current disposable-target execution policy or grant write
authority.

## Persistence, recovery, and deletion

The registry owns bounded Recipe/DataVersion lineage, application and
qualification projections, cutover selection, and restart-safe intents. The
protected Recipe store owns encrypted immutable Recipe and qualification
payloads with one vault-backed key per Recipe. Each DataVersion workspace owns
its separate DuckDB and project artifacts.

Publication, DataVersion creation, qualification, cutover selection, and
deletion enumeration cross storage boundaries through recoverable intents.
Standalone workspace deletion is allowed only for an unpublished bootstrap
Recipe with one DataVersion. Reusable or published Recipe deletion begins at
the Recipe boundary and persists the exact protected-key, workspace, and
registry target set before destructive cleanup.

## Current support boundary

Reusable Recipe publication and Test/Production application currently require
file-based source meaning and a remote Odoo application target. Odoo-origin
capture, pinned preparation, and same-database comparison are implemented as a
separate governed update path; they do not become a cross-database Recipe
application and cannot write back in the current product boundary.

## Related documentation

- [Recipe and data-version setup](../workflow/00-project-setup.md)
- [Contained project lifecycle](project-lifecycle.md)
- [Workflow evidence lifecycle](evidence-lifecycle.md)
- [Architecture overview](../../architecture/overview.md)
- [Security and infrastructure](../../architecture/security-and-infrastructure.md)
