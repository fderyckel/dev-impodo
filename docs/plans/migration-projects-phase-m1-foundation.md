# Migration Projects Phase M1 persistence foundation

## Status and authority

**Status:** Completed foundation phase from 2026-08-22.

Phase M2 superseded the three M1 storage generations with exact M2
generations. The M1 ownership, recovery, and reset decisions remain in force;
current generation names are recorded in the [Phase M2 source-package
foundation](migration-projects-phase-m2-source-packages.md).

This document records Phase M1 of the [Migration projects and multi-Recipe
cutover implementation
plan](migration-projects-and-multi-recipe-cutover-implementation-plan.md).
[ADR-014](../decisions/README.md#adr-014--migration-projects-coordinate-reusable-recipes-and-cutover-plans)
governs the ownership model, and the [Phase M0
contracts](migration-projects-phase-m0-contracts.md) remain the executable
architecture boundary.

Phase M1 supplies clean domain roots, repository ports, exact DuckDB schema
generations, and a development reset path. The local browser does not compose
these services yet. Its current Recipe-first workflow and user documentation
remain accurate until the later browser cutover phases pass.

## 1. Implemented outcome

Phase M1 introduces four separate business and technical identities:

- `MigrationProject` owns the governed migration effort.
- `DataVersion` identifies one Project-owned source package.
- `MigrationRun` coordinates one use of one DataVersion.
- `MigrationWorkspace` isolates one mapping and execution work area inside a
  run.

Creating a Project creates none of the other roots implicitly. A caller
creates each child explicitly with the current Project revision. Creating a
child advances that Project revision, so two concurrent commands cannot both
provision lineage from stale Project state.

`MigrationProjectService`, `DataVersionService`, `MigrationRunService`, and
`MigrationWorkspaceService` authorize commands and validate identifiers before
calling their repository ports. Every root keeps its own optimistic revision.
The new capabilities separate create and edit authority for DataVersions,
runs, and workspaces.

This phase does not accept source files, freeze a DataVersion, bind an Odoo
target, publish a Recipe, or migrate the current workspace services. Those
responsibilities begin in Phase M2 and later phases.

## 2. Exact local persistence

`MigrationFoundationDatabase` owns a separate clean storage boundary with this
layout:

```text
<storage-root>/
|-- registry.duckdb
`-- projects/<project_id>/
    |-- data_versions/<data_version_id>/data-version.duckdb
    `-- workspaces/<workspace_id>/workspace.duckdb
```

The databases use these exact schema generations:

| Database | Generation | Responsibility |
| --- | --- | --- |
| Registry | `impodo-migration-registry-2026-08-m1` | Bounded identity, lineage, status, intent, and audit projections for the target architecture |
| DataVersion store | `impodo-data-version-store-2026-08-m1` | Exact Project and DataVersion linkage for the future source-package boundary |
| MigrationWorkspace store | `impodo-migration-workspace-2026-08-m1` | Exact Project, DataVersion, run, workspace, and optional RecipeApplication linkage |

The registry creates the target Project, DataVersion, run, workspace, Recipe,
application, TargetBinding, CutoverPlan, qualification, selection, operation,
and event tables in one empty generation. It contains no
`project_registry`, `recipe_intent`, `recipe_workspace_linkage`,
`Recipe.current_data_version_id`, `Recipe.cutover_candidate_id`, or
Recipe-owned DataVersion field.

DuckDB prevents updates to a referenced parent row in cases where only a
non-key field changes. The registry therefore gives each mutable referenced
root an immutable identity table, including the Recipe, RecipeApplication, and
CutoverPlan roots that later phases will implement. Foreign keys point to those
stable identity records, while services update the root projections. This
preserves database-enforced existence and lets optimistic revisions advance
without deleting or weakening relationships.

Opening a DataVersion or workspace verifies its store generation and exact
linkage against the registry. An identifier supplied in the wrong namespace
raises a specific identifier-confusion error. Cross-Project parents, runs,
DataVersions, and application context fail closed.

## 3. Bounded access and recovery

The Project list reads one registry query. Common-table expressions count
DataVersions, runs, workspaces, and Recipes without opening child databases.
The focused gate creates 100 Projects and proves that list rendering calls no
DataVersion-store or workspace-store verifier. M1 introduces no Odoo request,
source-row loop, or per-Project child query.

Every create command accepts a stable operation UUID. The registry reserves a
hash-bound owner-specific intent before creating the root. An equivalent retry
returns the original identity; reuse with different request meaning, Project
revision, or actor fails. Fault injection verifies restart after intent
reservation, registry commit, and child-store creation without duplicate
roots or stores.

The repository writes actor issuer, subject, and display name to operation and
event records. It stores no password, API key, or Odoo credential.

## 4. Clean development reset

Startup accepts an empty database or the exact M1 generation. It rejects an
older Recipe-first or otherwise unexpected schema before creating child
directories and does not mutate the rejected database.

The reset command is developer-only and recoverable. First, a developer
reviews the exact plan:

```powershell
.\.venv\Scripts\python.exe scripts\reset-development-storage.py `
  --root "C:\exact\impodo\storage"
```

The command reports recognized targets, unknown entries, a content-bound
fingerprint, and the exact confirmation token. Unknown entries block all
moves. After reviewing the unchanged plan, the developer enables development
mode and supplies that exact token:

```powershell
$env:IMPODO_DEVELOPMENT_MODE = "1"
.\.venv\Scripts\python.exe scripts\reset-development-storage.py `
  --root "C:\exact\impodo\storage" `
  --confirm "RESET-MIGRATION-STORAGE:sha256:<reported-hash>"
```

The command moves only recognized Impodo entries into
`.impodo-development-reset/<reset-id>/`. It does not delete them. If the
storage changes after review, confirmation fails and the developer must review
a new plan.

## 5. Verification gate

[`tests/test_migration_project_phase_m1_foundation.py`](../../tests/test_migration_project_phase_m1_foundation.py)
proves:

- the exact registry and child-store generations;
- distinct identities and exact Project-owned relationships;
- a registry-only list for 100 Projects;
- authorization before creation;
- optimistic concurrency for every root;
- idempotent retries and changed-meaning rejection;
- restart-safe recovery at each persistence stage;
- identifier-namespace and cross-Project rejection;
- non-mutating rejection of Recipe-first storage; and
- review, confirmation, development-mode, unknown-entry, and quarantine rules
  for reset.

The M1 gate passes when these tests and the M0 contract suite pass together.
Phase M2 subsequently moved source-package ownership and renamed the current
internal workspace class without adding an old-schema reader or dual-write
path.
