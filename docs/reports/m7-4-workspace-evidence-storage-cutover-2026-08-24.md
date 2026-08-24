---
audience: developer
kind: report
status: current
---

# M7.4 workspace evidence and storage cutover

**Historical evidence:** This dated report records one completed delivery
slice. Current architecture and lifecycle contracts own behavior.

## Outcome

M7.4 gives every persisted evidence type the identity of its actual owner.
DataVersion source evidence uses `data_version_id`, workspace mapping and
operational evidence uses `workspace_id`, Project lineage retains
`project_id`, and shared run snapshots use `migration_run_id`.

The browser route and background-job vocabulary is intentionally left for
M7.5. M7.4 does not hide that remaining work behind aliases.

## Workspace engine

`WorkspaceState` now exposes `workspace_id`. The contained engine stores a
singleton `workspace_projection_cache` without a Project, DataVersion, run,
application, or workspace identity column. Registry `workspace_linkage`
remains the only cross-store identity statement.

Every workspace store or engine open verifies the complete Project,
workspace, DataVersion, MigrationRun, and optional Recipe-application tuple
before returning a path or reading evidence. A mismatch fails before the
contained database is exposed.

Workspace-owned domain payloads, repository ports, SQL columns, audit facts,
hash inputs, and fixtures now say `workspace_id`. Source selections,
source snapshots, Odoo capture selections, and Odoo origin manifests say
`data_version_id`. The evidence contract versions and the exact
`impodo-workspace-engine-2026-08-m7-4` generation changed with those hashed
keys.

Earlier or mixed engine tables, JSON shapes, and evidence versions are
rejected without mutation. M7.4 adds no upgrader, compatibility property,
fallback deserializer, or dual-write identity shape.

## Artifact ownership

`DataVersionSourceArtifactStore` and `WorkspaceArtifactStore` replace the
generic owner interface. The local adapter stores immutable source files,
source snapshots, and protected Odoo origins under
`artifacts/dv/<data_version_id>`. Prepared snapshots, derived values, reports,
and execution evidence use `artifacts/ws/<workspace_id>`.

The temporary DataVersion artifact adapter was deleted. Composition uses the
owner-specific ports directly, so a workspace UUID cannot silently select a
source root and a DataVersion UUID cannot select workspace evidence.

## Run evidence

The integrated planner no longer constructs a workspace evidence class with a
MigrationRun UUID. `MigrationRunTargetSchema` and
`MigrationRunReferenceBundle` are run-owned records. They bind the exact
source workspace provenance, canonical model or reference selection, and
`migration_run_id`. A per-application adapter projects only that application's
requirements into its real `workspace_id`.

The run classes reject unordered, duplicate, unavailable, or out-of-source
members. This preserves one shared capture without introducing an Odoo or
repository call per Recipe or source row.

## Verification

The following checks passed on 2026-08-24:

- the M7 identity, authorization, workspace-storage, and canonical-owner gate
  passed 23 tests;
- the complete M3 Project-authoring slice passed 9 tests;
- the integrated multi-Recipe M4 slice passed all 8 tests;
- execution snapshots, confirmed execution, and reconciliation passed 74
  tests; and
- mapping, normalization semantics, categorical coverage, Odoo capture,
  columnar compilation, Recipe representative shapes, and preparation
  capability passed 66 tests.

After the final contract review, the source-snapshot contract passed 18 tests,
the Odoo source-capture and publication boundary passed 24 tests, and the
documentation registry and code-documentation gate passed 7 tests. The
documentation quality command also completed without findings.

The repository-wide test command did not complete within its ten-minute
command limit and is not reported as passing. Eleven storage-heavy
normalization and Polars tests were also stopped by the known Windows `.tmp`
directory access gate before their assertions; the remaining tests in that
focused command reached product assertions and passed.

## Subsequent boundary

M7.5 subsequently renamed workspace route parameters, presenter and template
values, job lookup keys, and remaining fixtures. It preserved one bounded
workspace-to-Project authorization read per request and added no per-service,
per-dataset, per-row, or per-Odoo-record lookup.
