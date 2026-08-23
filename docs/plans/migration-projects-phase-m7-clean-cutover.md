# Migration Projects Phase M7 clean cutover

## Status

Completed on 2026-08-23.

M7 removes the superseded Recipe-first ownership path and makes the
Project-first model the only active runtime and documentation model. Historical
Recipe-first plans and reports remain only where they are clearly labelled as
point-in-time evidence.

## Operator outcome

A data manager creates a data project before any Recipe exists. The Project
owns its data deliveries, runs, workspaces, optional Recipes, and CutoverPlan.
The data manager may complete one-off migration work without saving a Recipe,
or may explicitly save eligible reusable rules as a Project-scoped Recipe.

The browser keeps these identities distinct:

- the Project groups one governed migration effort;
- a DataVersion identifies one complete accepted source delivery;
- a MigrationWorkspace keeps mutable work for one purpose; and
- a Recipe keeps reusable rules without source rows, credentials, approvals,
  or execution evidence.

## Implemented clean boundary

M7 removed the Recipe-root creation and ownership path, including its services,
repositories, storage adapters, routes, fixtures, and compatibility tests. The
browser now uses `/projects/new` only for Project creation and uses
`/workspaces/{workspace_id}` only for contained workspace work.

The current workspace engine uses `workspace-engine.duckdb`, `WorkspaceState`,
`WorkspaceStateService`, `WorkspaceStateRepository`, and
`WorkspaceStateReader`. New stores use the exact M7 schema generation. M7
removed the additive `project_schema_migration` ledger; an incompatible store
is rejected instead of upgraded or adopted.

The mapping boundary reads the DataVersion datasets selected for one workspace.
It overlays only that workspace's current derived-dataset plan and does not copy
source rows or artifacts into the workspace database. Canonical source-package
ordering keeps semantically identical requests hash-identical.

The browser navigation resolves the parent Project name from the registry and
does not present a workspace display name as the Project identity. Preparation
and Odoo-capture jobs also retain the parent Project name in their bounded
progress snapshots.

## Performance and safety review

The Project list and integrated-run pages continue to use bounded registry
projections. The mapping source adapter performs one bounded workspace
projection read and, only when derived rules exist, one package read plus one
derived-plan read. It performs no query or Odoo request per source row, field,
dataset, Recipe, or relationship.

Project creation, Recipe publication, run planning, qualification, and
Production activation retain restart-safe operation identities. Published
Recipe revisions, accepted DataVersions, selected CutoverPlans, and execution
evidence remain immutable and hash-bound.

## Documentation disposition

ADR-014 is implemented and supersedes ADR-012 and ADR-013. Current architecture,
workflow, developer, user, BPMN, and roadmap pages use the Project-first model.
The browser language and concept-help proposal remains an active separate
proposal and was not rewritten by M7.

## Verification

The M7 gate includes:

- focused M0 through M7 domain and persistence tests;
- the complete Project setup browser scenario class;
- workspace-engine and local-stack tests;
- repository-wide legacy and stale-semantic searches;
- Python compilation and formatting checks;
- documentation registry and quality tests; and
- `git diff --check` plus a final worktree review.

The exact executed checks and any environmental omissions belong in the final
implementation handoff rather than being inferred from this design record.
