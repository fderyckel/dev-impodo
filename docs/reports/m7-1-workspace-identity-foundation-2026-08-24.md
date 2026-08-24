---
audience: developer
kind: report
status: current
---

# M7.1 workspace identity foundation

**Historical evidence:** This dated report records one completed delivery
slice. Current architecture and lifecycle contracts own behavior.

## Outcome

M7.1 makes the intended identity model executable without changing the current
browser workflow. A workspace request can now resolve one immutable access
context containing the real Project, workspace, Data version, Migration run,
and optional Recipe application identities. The resolver verifies this lineage
in one registry query and authorizes the actor against the real parent Project
before a child store or external system can open.

The new boundary was a foundation for the later cutover. At M7.1 delivery,
workspace routes did not use it and still used the ambiguous `WorkspaceState`
contract. M7.2 subsequently consolidated canonical ownership, and M7.3 made
the resolver mandatory at workspace browser and background-job boundaries.

## Enforced contract

`WorkspaceAccessContext` has five explicitly named fields:

- `project_id` is only `MigrationProject.project_id`;
- `workspace_id` is only `MigrationWorkspace.workspace_id`;
- `data_version_id` is only the selected `DataVersion.data_version_id`;
- `migration_run_id` is only the containing `MigrationRun.migration_run_id`;
  and
- `recipe_application_id` is optional and identifies only the exact Recipe
  application linked to the workspace.

`MigrationFoundationRepository.resolve_workspace_access_context` reads the
registry once. Its joins prove that the Data version and run belong to the same
Project as the workspace, that the run uses that Data version, and that an
optional Recipe application matches all four identities. It does not call
`get_migration_workspace`, because that method also opens the workspace store.

`WorkspaceAccessService.resolve` first rejects an actor without the requested
capability, then resolves the lineage, rejects an adapter that substitutes a
different workspace identity, and finally checks Project membership through
the resolved `project_id`. The returned context can be reused by one request or
copied into one immutable worker packet. It must never be resolved once per
row.

## Executable semantic inventory

The M7 semantic test classifies all 68 source types that declare a typed
`project_id` field. Twenty-four use a real parent Project identity. Forty-four
still use `project_id` for workspace-local evidence and are explicitly listed
as M7 cutover debt. A new typed use fails the test until its meaning is
classified.

The same test records these current ambiguity budgets:

| Surface | M7.1 baseline | Meaning |
| --- | ---: | --- |
| Workspace route declarations using `{project_id}` | 74 | The parameter currently contains a workspace identity. |
| Workspace URL expressions using `project.project_id` | 134 | The template object is `WorkspaceState`, not a Migration Project. |
| Typed `WorkspaceState` values named `project` | 116 | The variable is a workspace-engine state object. |

Each budget may decrease but cannot increase. Later M7 slices must remove the
corresponding budget when the old surface reaches zero.

The inventory also registers each contract family that can change identity
meaning:

| Contract family | Current recorded surfaces | Required later action |
| --- | --- | --- |
| Hash-bound and portable workspace payloads | `SourceSelection`, `OdooModelCatalog`, `OdooSchemaCatalog`, `MappingWorkingDraft`, and the classified workspace-local evidence types | Rename workspace-owned keys to `workspace_id`, bump their exact contract versions, and regenerate evidence. |
| Persistence | Migration registry generation `impodo-migration-registry-2026-08-m6`; Data version store generation `impodo-data-version-store-2026-08-m2`; Migration workspace store generation `impodo-migration-workspace-2026-08-m2`; workspace-engine generation `impodo-workspace-engine-2026-08-m7`, version 2 | Keep the first three identity meanings. Replace the workspace engine's `project_id` identity row and start a new exact generation without an upgrader. |
| Operation requests | `MigrationOperationIntent`, Data version creation, run creation, workspace creation, and workspace-state commands | Keep the operation's real Project scope. Rename Project revision parameters currently called `expected_workspace_revision` to `expected_project_revision`; rename workspace-owned request fields separately. |
| Background work | `JobRequest`, `PreparationWorkspace`, `PreparationJob`, `LoadJob`, and `OdooCaptureJob` | Keep the already-correct `PreparationWorkspace` lineage packet. Replace job control keys that currently receive a workspace UUID as `project_id` with a verified workspace context. |
| Browser forms and links | Workspace routers and the workspace templates registered by `docs/workflow.yml` | Keep `/projects/{project_id}` for Project pages and cut workspace routes and view models over to `workspace_id` without a compatibility alias. |

## Adversarial authorization evidence

The focused tests create two Projects with distinct Data versions, runs, and
workspaces. A hosted-style policy grants one data manager access only to the
first Project. The first workspace resolves successfully; the second is denied
after its lineage is resolved and before a workspace store opens. Additional
checks prove that:

- a missing capability prevents any registry access;
- a Project UUID, unknown UUID, or malformed value cannot be used as a
  workspace UUID;
- a repository cannot substitute another workspace context; and
- the DuckDB adapter performs one registry read and no workspace-store read.

## Verification

- `tests/test_identity_semantics.py`
- `tests/test_workspace_access.py`
- `tests/test_migration_foundation.py`
- `tests/test_project_security.py`
- `scripts/documentation_quality.py --check`

## Subsequent boundary

M7.2 subsequently moved each retained business value to its canonical Project,
Data version, Migration run, or workspace owner. M7.3 then made
`WorkspaceAccessContext` mandatory at workspace application, browser, and
Odoo-worker boundaries. M7.4 subsequently replaced the ambiguous
workspace-local `project_id` persistence and payload shapes, split artifact
ownership, and made mixed evidence generations fail closed.
