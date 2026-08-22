---
audience: developer
kind: contract
status: current
---

# Project and workspace lifecycle contract

## Scope

`MigrationProject` is the operator-facing business and governance root. It
owns DataVersion, MigrationRun, MigrationWorkspace, and Recipe membership
lineages. A Project can exist and complete one-off work without a Recipe.

`MigrationWorkspace` is the isolated technical work area for one run over one
DataVersion. The contained mapping engine currently represents its detailed
state as `WorkspaceState`; that type is not the Project aggregate.

## Creation

`/projects/new` creates four distinct roots in this order:

1. one Project;
2. Authoring DataVersion 1 and its draft source package;
3. Authoring MigrationRun 1; and
4. one open MigrationWorkspace plus its contained mapping engine.

The browser operation and deterministic child operation IDs make this
coordinator restart safe. A replay with the same request meaning returns the
existing roots. A replay with different meaning fails closed.

Creation does not add a Recipe, contact Odoo, inspect source rows, or grant
write authority.

## Workspace setup

The mapping engine setup selects one source mode: uploaded files (`FILE`) or
existing records in one Odoo database (`ODOO`). The detailed engine moves from
`DRAFT` to `REGISTERED`; the clean MigrationWorkspace remains `OPEN` while it
accepts authoring evidence.

File setup requires at least one governed CSV or XLSX file before
registration. Odoo-source setup requires an exact connection identity and the
purpose-specific read check. Registration records actor-bound evidence and
does not publish a Recipe.

## Source ownership

Source bytes, inspection catalogues, accepted parsing choices, logical
datasets, and immutable snapshot references become one DataVersion source
package. Freezing that package makes the DataVersion immutable.

The clean MigrationWorkspace store records only its projection ID, package
hash, selected dataset IDs, and snapshot hashes. The mapping engine receives a
read-only `SourceSelection` adapter over those references. It must not copy the
DataVersion database or source rows.

## Authority and concurrency

Every command receives a verified actor and capability. Human-entered manager
or owner names are governance metadata, not authorization. Project,
DataVersion, run, workspace, and Recipe changes use optimistic revisions and
reject stale forms.

Credentials stay in role-specific vault entries. They never become Project,
DataVersion, Recipe, mapping, or approval meaning. Read capability never grants
write capability.

## Persistence and performance

The registry lists Projects and their bounded counts without opening one
workspace database per list row. DataVersion and workspace databases use exact
schema generations and exact linkage. Storage from the superseded Recipe-first
generation is rejected without mutation.

Preparation workers receive an exact authorized identity packet and verify the
workspace and frozen DataVersion stores. They do not open or scan the shared
registry and do not issue per-row repository or Odoo calls.

## Current boundary

M3 supports Project-native authoring, one-off completion, and optional Recipe
publication. Phase M4 owns multi-Recipe application inside a run. No current
browser path treats a Recipe as the Project or gives a Recipe ownership of a
DataVersion.

## Verification

- `tests/test_migration_project_phase_m1_foundation.py`
- `tests/test_migration_project_phase_m2_source_packages.py`
- `tests/test_migration_project_phase_m3_project_authoring.py`
- `tests/test_preparation_jobs.py`

## Related documentation

- [Recipe publication contract](recipe-lifecycle.md)
- [Evidence lifecycle](evidence-lifecycle.md)
- [Project setup implementation](../workflow/00-project-setup.md)
- [Architecture overview](../../architecture/overview.md)
