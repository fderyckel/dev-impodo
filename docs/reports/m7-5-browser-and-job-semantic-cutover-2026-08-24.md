---
audience: developer
kind: report
status: current
---

# M7.5 browser and job semantic cutover

**Historical evidence:** This dated report records one completed delivery
slice. Current architecture and lifecycle contracts own behavior.

## Outcome

M7.5 removes the remaining browser and background-job aliases that called a
workspace a Project. Project pages use `/projects/{project_id}` and workspace
pages use `/workspaces/{workspace_id}`. Workspace templates receive explicit
`migration_project`, `migration_workspace`, `data_version`, `migration_run`,
and workspace-evidence values instead of a generic `project` value.

Preparation, Odoo-capture, and load jobs now use `workspace_id` for enqueue,
lookup, progress, cancellation, retry, and history. A Project display name is
stored separately as `migration_project_name`; it is never used as an identity.
Machine-local Odoo readiness and process ownership also use `workspace_id` and
`forget_workspace`, because that browser-session state belongs to one
workspace. No old route, job field, compatibility property, or fallback lookup
remains.

## Authorization and bounded reads

A normal workspace request resolves one verified lineage row from the
registry, authorizes the actor against its parent `project_id`, and reuses that
context for the complete request. Services and per-row work do not resolve the
Project again.

A background progress request reuses the immutable lineage packet captured
before its job was queued. This keeps progress available while a worker owns a
DuckDB file and avoids another registry read. The middleware still checks the
requesting actor's Project capability and rejects a packet whose
`workspace_id` does not match the route. Unknown jobs fall back to the normal
opaque workspace authorization path.

This is the deliberate N+1 control: one normal request performs one bounded
lineage read, and one verified job request performs none. No service, dataset,
source row, or Odoo record adds another authorization lookup.

## Browser and test vocabulary

Workspace route parameters, handler variables, presenter arguments, template
links, and background-job fields use workspace language. Workspace-only
repository ports and test fixtures no longer use names such as
`ProjectReader`, `ProjectRepository`, or `project: WorkspaceState`.

Visible text was changed only where the old wording assigned ownership to the
wrong object. Files belong to the Data version, mapping and operational
evidence belong to the workspace, and Odoo destination setup belongs to the
Migration run. The separate browser-language proposal remains untouched.

The executable semantic gate now requires zero workspace routes using
`project_id`, zero workspace links constructed from `project.project_id`, zero
typed `WorkspaceState` values named `project`, and no background job with a
`project_id` or `project_name` alias. Wrong-kind UUIDs and mismatched verified
job packets fail closed.

## Verification

The following focused checks passed on 2026-08-24:

- 41 identity, authorization, job, and hosting-contract tests, including 12
  executable semantic guards and verified-job reuse without a second registry
  read;
- 15 local-stack tests;
- 111 storage-focused regression tests;
- 33 readiness, preflight, and execution-snapshot tests; and
- 7 documentation ownership and code-documentation tests.

The focused browser routes for Project creation, workspace setup, resume, load
progress, confirmed background load, and locked preparation progress passed.
The registration scenario reached its final manifest assertion and exposed a
stale expected contract version of 5 while the stored contract was version 6.
The expectation was corrected, but its isolated rerun reached the 124-second
command limit without a result and is not counted as a passing rerun.

The first full browser-suite command reached the command time limit without a
result. The first focused browser attempt was blocked before application code
by the known Windows temporary-directory access gate. The focused browser
checks were then run outside that sandbox gate and reached product assertions.

## Subsequent closure

M7.6 subsequently synchronized every current authority, removed completed M7
delivery detail from the plan, removed retained compatibility code and stale
test vocabulary, and passed the final repository-wide semantic inventory.
