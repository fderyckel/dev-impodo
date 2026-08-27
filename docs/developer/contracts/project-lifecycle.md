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
state through a flat `WorkspaceState` workbench projection. That type is not an
identity, aggregate root, or lifecycle owner.

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

The DataVersion package selects one source origin: uploaded files (`FILE`) or
existing records in one Odoo database (`ODOO`). The clean
`MigrationWorkspace` moves from setup `DRAFT` to `READY` and remains `OPEN`
while it accepts authoring evidence. The mapping engine has no second setup
lifecycle.

File setup requires at least one governed CSV or XLSX file before
registration. Odoo-source setup requires an exact connection identity and the
purpose-specific read check. Registration records actor-bound evidence and
does not publish a Recipe.

## Source ownership

Source bytes, inspection catalogues, accepted parsing choices, logical
datasets, and immutable snapshot references belong to one DataVersion source
package from the first draft file onward. Freezing that package makes the
DataVersion immutable.

The clean MigrationWorkspace store records only its projection ID, package
hash, selected dataset IDs, and snapshot hashes. The mapping engine receives a
read-only `SourceSelection` adapter over those references. It must not copy the
DataVersion database or source rows.

The MigrationRun owns one mutable `MigrationRunTargetSetup` revision before
target capture and one immutable `TargetBinding` afterward. Workspaces in the
same run cannot own or diverge from that target context.

## Authority and concurrency

Every command receives a verified actor and capability. Human-entered manager
or owner names are governance metadata, not authorization. Project,
DataVersion, run, workspace, and Recipe changes use optimistic revisions and
reject stale forms.

Credentials stay in role-specific vault entries. They never become Project,
DataVersion, Recipe, mapping, or approval meaning. Read capability never grants
write capability.

A read-only `WorkspaceAccessContext` resolver verifies the Project, workspace,
DataVersion, MigrationRun, and optional RecipeApplication lineage in one
registry query. It authorizes the requested capability against the resolved
parent Project before a workspace store, DataVersion store, protected
artifact, credential, or Odoo boundary may open.

Every authenticated workspace request binds that verified context before route
code runs. Workspace services and Odoo workers reuse it. Missing, wrong-kind,
mismatched, and inaccessible identities stop before child stores or external
adapters open. The contained workbench cache stores no identity columns. A
normal request performs one bounded lineage read; a progress request reuses
the verified job packet without reopening the registry.

## Persistence and performance

The registry lists Projects and their bounded counts without opening one
workspace database per list row. DataVersion and workspace databases use
explicit schema generations, versions, and exact linkage. Impodo upgrades a
supported older version within the current generation in one transaction,
records each consecutive step, and validates the exact current shape before a
repository can use that database. It does not run migrations per domain row
and does not contact Odoo.

Each database upgrades independently. If a process stops between databases,
every committed database remains current and every untouched database remains
a valid supported older version. The next authorized open resumes the
remaining work. A failed database transaction keeps its prior version and
data unchanged.

A different generation, a version below the supported baseline, a version
newer than the application, a missing migration step, or a malformed shape is
rejected without mutation. Storage from the superseded Recipe-first generation
still requires the reviewed development reset. Repositories contain no dual
read, dual-write, alias, or downgrade path.

Storage-schema migration does not rewrite hash-bound source packages, Recipe
revisions, snapshots, approvals, or execution evidence. A future semantic
payload change must retain an explicit decoder for supported old payloads or
create a new immutable successor revision; changing a schema version alone
cannot reinterpret that evidence.

Preparation workers receive an exact authorized identity packet and verify the
workspace and frozen DataVersion stores. The packet also binds the application
build identifier and expected workspace schema contract; a mismatch stops the
worker before either store is opened. Workers do not open or scan the shared
registry and do not issue per-row repository or Odoo calls.

## Current boundary

Project-native authoring, one-off completion, optional Recipe publication,
integrated Test qualification, and Production orchestration are implemented.
Each RecipeApplication has an isolated workspace while the run owns shared
target evidence. No current browser path treats a Recipe as the Project or
gives a Recipe ownership of a DataVersion.

## Verification

- `tests/integration/duckdb/test_migration_foundation.py`
- `tests/integration/duckdb/test_forward_upgrades.py`
- `tests/application/data_version/test_source_packages.py`
- `tests/application/project/test_authoring.py`
- `tests/application/run/test_integrated_recipe_runs.py`
- `tests/architecture/test_identity_semantics.py`
- `tests/application/workspace/test_access.py`
- `tests/architecture/test_canonical_ownership.py`
- `tests/application/workspace/preparation/test_jobs.py`

## Related documentation

- [Recipe publication contract](recipe-lifecycle.md)
- [Evidence lifecycle](evidence-lifecycle.md)
- [Integrated Test run lifecycle](integrated-run-lifecycle.md)
- [Project setup implementation](../workflow/00-project-setup.md)
- [Architecture overview](../../architecture/overview.md)
