---
audience: developer
kind: plan
status: current
---

# Shared-server Phase 0 baseline

## Scope

This report records the local Impodo baseline observed on 2026-10-22 before
shared-server persistence or identity implementation. It is point-in-time
evidence, not a permanent performance guarantee. The target architecture is in
the
[shared-server implementation plan](../plans/linux-server-postgresql-duckdb-implementation.md)
and the then-proposed
[fact-ownership inventory](../architecture/server-fact-ownership.md).

No supported PostgreSQL server composition, Organization administration,
shared session, Linux container deployment, or durable worker existed at this
baseline. Phase 1 had not begun.

## Source and runtime identity

| Item | Observed value |
| --- | --- |
| Branch | `main`, tracking `origin/main` |
| Base commit | `2632208e3008c08cf21a3d45e711d2b8ae558fa2` |
| Host observed | Microsoft Windows NT 10.0.26200.0; Windows PowerShell 5.1.26100.9711 |
| Python | CPython 3.14.7 from `.venv` |
| Supported current product profile | Local Windows application |
| `pyproject.toml` SHA-256 | `BFEB133F4D303C06F8D49865BF2EB71C7D9EF1D9CA622086F438699C1971FD58` |
| `uv.lock` SHA-256 | `FF304A17849D6EAFA5DA94973F013E8827D0EDB229340306229565A305532239` |

At baseline capture, the worktree already contained the architecture-plan
documentation being developed for this initiative:

- modified `docs/README.md`;
- modified `docs/plans/vm-deployment-duckdb-worker-management.md`; and
- untracked `docs/plans/linux-server-postgresql-duckdb-implementation.md`.

Those files were not treated as product runtime behavior. No unrelated dirty
file was overwritten.

## Current composition boundary

`src/impodo/web/app.py::create_local_app` constructs the local graph. The
current `WebContext` contains one fixed actor together with broad migration,
job, local-stack, target, and evidence services. Application services already
receive authorization policies and stable actor identities, but Project list
queries and the current repository port are not Organization-scoped.

`LOCAL_ACTOR` currently has every `Capability`. Production imports are limited
to the local web composition and the scenario profile workflow. Phase 0 adds an
architecture guard so new shared code cannot import it. Resolving an actor per
request and splitting composition roots remain Phase 1 work.

## Current local storage layout

The migration foundation creates this contained layout:

```text
<project-root>/
  registry.duckdb
  artifacts/
  .project-evidence-protected/
  .recipes-protected/
  projects/
    <project-id>/
      data_versions/
        <data-version-id>/
          data-version.duckdb
      workspaces/
        <workspace-id>/
          workspace.duckdb
          workspace-engine.duckdb
```

The exact artifact subtrees vary by completed workflow. Paths remain contained
under the configured root. Current compatibility checks run when a store is
opened and may apply supported forward-only schema upgrades; this baseline did
not open historical customer stores.

### Store generations

| Store | Generation | Baseline | Current version |
| --- | --- | ---: | ---: |
| Migration registry | `impodo-migration-registry-2026-08-project-root` | 1 | 7 |
| Data version | `impodo-data-version-store-2026-08-project-owned` | 1 | 2 |
| Workspace reference | `impodo-migration-workspace-2026-08-reference-only` | 1 | 2 |
| Workspace engine | `impodo-workspace-engine-2026-08-workspace-owned` | 1 | 16 |

### Representative sanitized store sizes

No supported live Project root was supplied for this capture. Read-only file
metadata from three existing diagnostic copies under `.codex-artifacts` gave
these indicative sizes; no DuckDB file was opened or upgraded.

| File | Samples | Minimum | Maximum |
| --- | ---: | ---: | ---: |
| `data-version.duckdb` | 2 | 5,255,168 bytes | 8,400,896 bytes |
| `workspace.duckdb` | 3 | 2,895,872 bytes | 2,895,872 bytes |
| `workspace-engine.duckdb` | 3 | 17,575,936 bytes | 22,818,816 bytes |

These copies are not a capacity benchmark. They show only that the current
layout uses several owner-qualified DuckDB files and that worker scheduling,
backup, and deletion cannot account for only `registry.duckdb`.

## Current job behavior

Current long-running workflows are implemented by several process-local
managers rather than one durable queue:

| Manager or dispatcher | Current lifecycle and recovery boundary |
| --- | --- |
| `InlineJobDispatcher` | Executes synchronously in process. Keeps job and idempotency dictionaries in memory. Supports queued, running, succeeded, and failed records; no durable restart recovery. |
| `CorrectionJobManager` | Uses one daemon `SerialJobRunner`; queued and current process state are not a shared-server queue. |
| `PreflightJobManager` | Owns its process-local lifecycle and progress; no PostgreSQL attempt or lease exists. |
| `RecipeRunJobManager` | Uses a daemon serial runner and process-local lifecycle. |
| `OdooCaptureJobManager` | Supports cooperative cancellation checkpoints for bounded capture work. Registry state is process-local. |
| `PreparationJobManager` and `PreparationJobRegistry` | Support queued/running progress, review-required, succeeded, failed, cancellation requested, and cancelled states. Active control is process-local, while selected preparation evidence has DuckDB recovery support. |
| `LoadJobManager` | Tracks queued, running, succeeded, and failed load work in process-local job state; governed execution evidence remains separately persisted. |

`SerialJobRunner.shutdown()` discards queued tasks, does not interrupt the task
already running, and joins its daemon worker only briefly. Current managers may
recover particular persisted workflow evidence after restart, but there is no
single durable job request, attempt, heartbeat, lease, cancellation, and result
authority across processes. Phase 0 contracts describe that future boundary
without changing these managers.

## Focused verification before Phase 0 changes

The following results established the working local baseline. Windows sandbox
ACL restrictions initially blocked temporary DuckDB fixtures; the relevant
test command was rerun with the established required permission and passed.

| Command or suite | Result |
| --- | --- |
| `python -m unittest tests.architecture.test_dependency_rules` | Passed. |
| `python -m unittest tests.architecture.test_hosting_contracts tests.integration.duckdb.test_page_read_connections -v` | 11 tests passed in 2.357 seconds. |
| `python -m unittest tests.application.workspace.preparation.test_jobs -v` | 16 tests passed in 0.470 seconds. |
| `python -m unittest tests.application.project.test_authoring -v` | The broad invocation reached its 240-second command limit after 15 of 18 tests had passed. The remaining three tests were run explicitly and passed in 39.066 seconds. All 18 tests therefore passed across the partitioned run; the timeout was not recorded as a product failure. |

The exact elapsed times depend on the Windows host, filesystem, security
scanner, and test partition. They are diagnostic evidence only.

## Live performance evidence omitted

No Impodo Python process was running when the baseline was captured. Therefore
Phase 0 did not manufacture route timings or connection counts from old logs.
The following live evidence is explicitly absent:

- browser route elapsed time for Projects, Project home, Data version, run, and
  Workspace pages;
- physical DuckDB connections and schema-check counts for those live requests;
- request-scoped retry and database-busy behavior under a current browser
  session; and
- a live store-size and disk-headroom measurement from an active Project root.

The existing `test_page_read_connections` contract passed, but it is not a
substitute for a route-specific before/after measurement. Phase 1 must capture
those metrics for each changed local route using one `run_page_read` scope and
must not keep a DuckDB connection open across an await boundary.

## Phase 0 additions recorded after baseline

Phase 0 adds portable, immutable contracts without composing them into the
local application:

- Organization identity, authenticated principal, fixed request scope,
  Project visibility, and effective authorization receipt;
- storage-neutral artifact identity, semantic operation request, Workspace
  lease and fencing token, data-plane receipt, projection receipt, and safe
  optimistic-conflict result;
- durable job request, attempt, transition, progress, cancellation,
  interruption, and result identities; and
- Project retention choice, deletion request and result, and minimal deletion
  receipt.

The local runtime continued to use its existing actors, stores, and managers
at baseline capture. The fact-ownership inventory was still proposed at that
time.

The inventory was accepted on 2026-10-23 when the user instructed development
to start the next phase. Phase 1 therefore starts from this report while
retaining its explicit omissions; the historical baseline is not rewritten as
if server behavior or live performance evidence existed on 2026-10-22.

### Phase 0 verification

| Suite | Result |
| --- | --- |
| Dependency rules and new server contracts | 13 tests passed in 29.018 seconds. |
| Documentation quality and Python documentation inventory | 8 tests passed in 33.810 seconds. |
| Existing artifact, job, and approval integration coverage | 27 tests passed in 82.069 seconds; 2 opt-in 50,000-row qualifications skipped. The default sandbox run failed at protected temporary-directory creation, and the exact suite passed with the established Windows test permission. |
| Existing hosting and page-read connection contracts | 11 tests passed in 3.214 seconds with the established Windows test permission. |
| Python compilation and `git diff --check` | Passed. Only Git's existing LF-to-CRLF worktree warnings were reported. |
