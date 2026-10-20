---
name: impodo-duckdb-performance
description: Diagnose, implement, and verify Impodo web-route and workflow performance without weakening persisted evidence or database safety. Use when a page or command is slow, DuckDB opens or schema checks may be repeated, a route/service/repository orchestration is added or changed, a performance regression is suspected, or coding work should receive a database-lifetime performance check even when performance is not the primary request.
---

# Impodo DuckDB Performance

Measure the current request path before changing it, then keep database ownership bounded to one synchronous local unit of work.

## Diagnose first

1. Preserve unrelated worktree edits and the running Impodo instance.
2. Prefer the privacy-safe diagnostic log for live timings. Do not benchmark by opening live DuckDB files from a second process.
3. Record, for the exact route and state:
   - total request duration;
   - `database_connection_count`;
   - `database_schema_check_count`;
   - lock retries and lock-wait time;
   - `db_connect`, `db_schema`, view/projection, navigation, hashing, and template-render time when available.
4. Trace the route through presenters, services, and repositories. Identify repeated reads and distinguish local database work from file, remote Odoo, background, and browser work.
5. State the measured bottleneck. Do not recommend a broad rewrite when connection setup, schema work, hashing, or rendering has not been isolated.

Use `C:\Users\francois.deRyckel\AppData\Local\Impodo\diagnostics\impodo.jsonl*` for a running internal build when available. Treat live timings as current only after Impodo has restarted on the code under review.

## Choose the ownership boundary

- Use `run_page_read()` around one complete synchronous local page projection and render.
- Use `run_local_operation()` around one bounded synchronous local command.
- Keep retained owners on the same worker thread.
- Release retained owners before any `await`, remote Odoo call, background job, redirect, or unrelated operation.
- Parse asynchronous forms and network input outside a retained scope. A local, synchronous intake or inspection command may own its own bounded operation scope.
- Prefer one bulk repository read over one connection per key or row.
- Consolidate duplicate service reads after the physical connection churn is bounded; preserve authorization, stale-evidence checks, and transaction independence.
- Never introduce a process-wide or cross-request DuckDB connection cache.

## Add a regression gate

Use `collect_duckdb_request_timings()` in a route-level test that exercises the action-rich state which exposed the problem.

- Assert the rendered or mutated business result as well as performance structure.
- Set a connection ceiling from the expected distinct database path/configuration owners and command scopes. Do not copy a ceiling from an unrelated route.
- Cover materially different states such as empty, uploaded, registered, or action-rich when they traverse different repositories.
- Bound schema checks when repeated schema work is part of the risk.
- Avoid wall-clock assertions in CI; Windows host variance makes connection-count and query-shape assertions more reliable.
- On `WinError 5` in disposable `.tmp` fixtures, rerun with the established required permission before treating it as a product failure.

## Verify and report

1. Run the focused route and repository tests.
2. Run adjacent workflow tests for every shared helper changed.
3. Run compilation/static checks and `git diff --check`.
4. Restart Impodo and retime the same live route and workflow state.
5. Report before/after duration, connections, schema checks, lock retries, and the dominant timing component. Label fixture evidence separately from live evidence.

Do not claim a live latency improvement from tests alone. Do not weaken evidence validation, authorization, or recovery behavior to meet a performance target.
