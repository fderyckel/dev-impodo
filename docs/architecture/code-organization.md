---
audience: developer
kind: architecture
status: current
---

# Code organization

Impodo groups Python code by layer, then by the business owner or capability it
serves. Use this guide to locate the state a change affects, find the code that
owns it, and select the tests that cover it.

## Begin with the owner

A Project groups Data versions, Migration runs, Workspaces, Recipes, and
Cutover plans. Each Workspace uses one Data version and belongs to one run. An
authoring Workspace can stand on its own within that run. When a run applies a
Recipe revision, each Recipe application has its own Workspace.

| Owner | State it owns | Main code paths |
| --- | --- | --- |
| Project | Identity, status, membership, governance, and selection of a qualified plan for rollout. | `domain/project`, `application/project` |
| Data version | Source files, detected file structure, confirmed parsing choices, datasets, snapshots, and protected source references. | `domain/data_version`, `application/data_version` |
| Migration run | Target binding, combined requirements, application order, shared status, and integrated results. | `domain/run`, `application/run` |
| Migration workspace | Selected dataset and snapshot references, mapping decisions, prepared data, comparison results, execution journal, and reconciliation evidence. | `domain/workspace`, `application/workspace` |
| Recipe | Portable rules, immutable revisions, and revision history. | `domain/recipe`, `application/recipe`, `domain/recipe_envelope.py`, `domain/recipe_applications.py` |
| Cutover plan | Versioned dependencies, write ownership, controls, and qualification. | `domain/cutover`, `application/cutover_plan_service.py`, `application/production_cutover_service.py` |

The registry owns Workspace identity, setup state (`DRAFT` or `READY`), and
lifecycle (`OPEN` or `CLOSED`). The workbench in `domain/workspace/workbench.py`
represents the mapping engine's working state inside that Workspace. The
Workspace reads selected source data through references to its Data version.
A Recipe revision carries reusable rules. Each run and Workspace keep their
own approvals and results.

## Packages by layer

| Layer | What belongs there |
| --- | --- |
| `domain` | Business models, rules, and calculations. Owner packages sit alongside pure capability packages such as `mapping`, `compiler`, `preparation`, `staging`, `preflight`, and `execution`. |
| `application` | Use cases and the interfaces they need from storage, Odoo, and other services. `workspace` separates mapping, preparation, and execution. Named coordinators at the layer root handle actions that span owners. |
| `adapters` | Implementations for DuckDB, PostgreSQL, Odoo, artifacts, jobs, protected evidence, identity, secrets, and scenario files. DuckDB repositories are grouped by storage technology and named for the state or evidence they handle. |
| `web` | Request handling, presentation, and application assembly. Routers accept requests, presenters build views, and composition selects the adapters and runtimes. Templates and browser assets stay with web delivery. |

Identity and scenario code follow the same layering. Each has domain contracts,
application services, and adapters. The `src/impodo` package root contains only
`__init__.py` and the `python -m impodo` entry point.

## Mapping, preparation, and execution

| Capability | Business rules and application entry points |
| --- | --- |
| Mapping | `domain/mapping` defines rule values and validation. `domain/compiler` compiles supplied contracts. `application/workspace/mapping` edits and validates workspace decisions. The Recipe compilation and publication services turn accepted decisions into portable rules. |
| Preparation | `domain/preparation` and `domain/staging` evaluate supplied transformation, quality, and staging contracts. `application/workspace/preparation` owns the use cases. DuckDB collaborators persist results behind `PreparationSessionRepository`. `preparation_job_manager.py` and `preparation_worker.py` in `web/composition` run the local preparation process. |
| Execution | `domain/execution` defines plans, write requests, and read-back results. `application/run` coordinates the applications in a run. `application/workspace/execution` loads and reconciles one application. DuckDB stores its journal. `target_readers.py` and `target_writers.py` in `web/composition` select the Odoo adapters. |

Pure functions can carry owner IDs to preserve lineage. Application use cases
resolve those IDs through interfaces that specify which owner they can read or
change.

## Dependency direction

| Layer | Dependency rule |
| --- | --- |
| `domain` | Uses the standard library, reviewed pure libraries, and domain contracts. Keep framework, storage, credential, and job runtime details outside this layer. |
| `application` | Uses domain contracts and defines the interfaces its use cases need. Concrete adapters and browser code stay outside this layer. |
| `adapters` | Implement application interfaces using domain contracts. Keep routes, templates, and mutable browser context out of adapters. |
| `web/routers` and `web/presenters` | Use application services, read models, and view contracts. Composition supplies repositories, protected stores, vaults, and Odoo adapters. |
| `web/composition` and approved entry points | Build application services from concrete adapters and runtime configuration. Domain rules and use-case decisions stay in their owning layers. |

The dependency tests check layer imports, runtime cycles, and where concrete
adapters are constructed. The inventory tests check module classification. The
repository gate reports module and import counts and digests. See the [dependency
tests](../../tests/architecture/test_dependency_rules.py) and [inventory
tests](../../tests/architecture/test_inventory.py).

## Ports and transaction boundaries

Application ports name the operation or evidence a use case needs. For example,
a command that changes several records in the local registry exposes one atomic
operation. The DuckDB adapter opens the connection, shares it with its private
collaborators, and commits or rolls back. The application service receives the
result without handling the connection or its transaction.

`MigrationFoundationRepository` is the local registry facade. Its Project, Data
version, run, Workspace, operation-intent, and source package collaborators
share `RegistryTransactionCoordinator`. See the [registry
facade](../../src/impodo/adapters/duckdb/migration_foundation_repository.py) and
[transaction coordinator](../../src/impodo/adapters/duckdb/registry_transaction.py).

The local registry, protected artifacts, Data version database, and Workspace
databases commit separately. When a command spans stores, its operation intent
binds the identity to the exact request. Retrying the same request resumes or
returns the same result. Reusing that identity with different input is a
conflict. The Linux server uses separate authority rules for PostgreSQL and
DuckDB. See [Shared-server fact ownership](server-fact-ownership.md).

Keep I/O bounded when splitting repositories or interfaces. Preserve bounded
Project listings and Workspace reads, and keep Odoo requests, writes, and
read-back in batches.

## Local browser database operation scopes

Keep synchronous local database work off the FastAPI event loop. Run each
complete local phase in one synchronous callback on one worker.

| Work | Helper | What runs in the callback |
| --- | --- | --- |
| Render a local page | `run_page_read` | Repository reads, navigation, presenter work, and template rendering. |
| Render the file upload page, Source data, saved Odoo details, Match data, or Final review | `run_workspace_page_read` | Workspace authorization and journey checks, followed by page reads and rendering, inside one `run_page_read` callback. |
| Execute a local command | `run_local_operation` | The command's repository transactions and any local read-back needed for its response. |
| Execute a local phase from an existing worker | `run_local_operation_sync` | One synchronous phase on that worker. |

See the [database scope helpers](../../src/impodo/web/composition/page_reads.py)
and [workspace admission middleware](../../src/impodo/web/security.py).

The five saved pages use `WorkspaceAccessMiddleware` to authorize the actual
parent Project and apply closed-Workspace and Recipe-run journey rules before
reading child data or inspecting active jobs. Match data checks source
readiness in the same operation. The middleware rejects a successful response
that skipped admission. Other workspace routes resolve access in a separate
bounded operation before calling the route.

Keep application mutations out of page reads. Repository-backed authorization
runs inside a bounded local operation. Repositories can reuse open DuckDB
owners within a callback and keep their transactions independent. Release all
retained owners before awaiting other work, streaming a response, contacting
Odoo, or starting a background job. A second request validates the stores it
opens independently.

File intake and inspection capture local state, validate or inspect files
outside the database scope, then publish in a new local scope. A spawned file
worker checks that it loaded the same application build as its caller.
Publication rechecks authorization, lifecycle, and captured state. The
application's `LocalOperation` port lets composition supply
`run_local_operation_sync` for the database phases. See the [file
worker](../../src/impodo/application/data_version/source_worker.py) and [local
operation port](../../src/impodo/application/shared/local_operations.py).

On the first access to a retained store, `ensure_current_schema` validates its
exact schema and applies any supported upgrade. The registry, Data version,
Workspace linkage, and Workspace engine stores each check their own contract.
`validate_schema_once_for_operation` reuses only a successful check for the same
database path, connection configuration, and schema contract. Every access
still checks the requested Workspace or Data version lineage. Diagnostics
report checks across all four store contracts. See [schema
validation](../../src/impodo/adapters/duckdb/schema/forward_upgrades.py) and the
[DuckDB operation scope](../../src/impodo/adapters/duckdb/unit_of_work.py).

For a new or materially changed database-backed route, measure elapsed time,
physical DuckDB opens, connection time, SQL statements including catalogue
queries, schema checks, and lock retries. Use paired fixtures and the pinned
toolchain to measure latency. Confirm a live performance claim in a restarted
build. Preserve hashes that identify evidence, operations, and invalidated
results.

Focused tests should cover one off-loop worker, the full read or command
boundary, and connection and SQL limits derived from the fixture. Cover saved
results on success, unchanged evidence on rejected forms, and owner release
before remote calls or jobs. For file intake, cover first inspection, repeat
checks, changed parsing choices, and rejected publication. The test helper
`count_duckdb_statements` preserves retained cursor identity so the probe
measures the same schema reuse as the application. See the [worker
scope](../../tests/integration/web/test_page_reads.py), [DuckDB
lifecycle](../../tests/integration/duckdb/test_page_read_connections.py),
[store validation](../../tests/integration/duckdb/test_scoped_store_validation.py),
[workspace admission](../../tests/integration/web/test_workspace_page_admission.py),
and [file intake](../../tests/integration/web/test_file_intake_performance.py)
tests.

## Browser files and assets

`web/composition/service_graph.py` assembles services from injected interfaces.
The local and server entry points choose their concrete stores and runtimes.
Route contexts expose the capabilities each router needs.

Templates hold server-rendered pages and fragments. Match data composes its
fragments under `web/templates/mapping`. Shared browser behavior lives in
`web/static/app.js`. Pages load their own workflow scripts and styles alongside
the shared tokens, layout, components, and workflow styles.

## Test organization

| Location | What it covers |
| --- | --- |
| `tests/architecture` | Portable architecture and documentation rules. |
| `tests/domain` and `tests/application` | Business rules and owner-specific use cases. |
| `tests/integration` | Boundaries such as databases, artifacts, Odoo, and browser routes. |
| `tests/e2e` | Complete workflows. |
| `tests/performance` | Measured scale contracts. |
| `tests/platform/<platform>` | Host-specific behavior, run in the owning platform suite. |
| `tests/support` | Shared builders and probes. Builders take explicit owner IDs, and tests can use `tests.support.paths.REPOSITORY_ROOT` to locate repository files. |

## Placement procedure

1. Identify the state or evidence the change reads or writes, then find its owner.
2. Choose the layer and existing owner or capability package. Use a module
   directly under a layer only for a cohesive shared contract, a cross-owner
   coordinator, or a stable facade.
3. Give cross-owner sequencing to an application coordinator. Keep owner
   reads and commands behind narrow ports, and keep technology details in
   adapters and composition.
4. Select the focused tests. Check for unnecessary imports, shared route
   capabilities, or extra queries introduced by the change. Update the
   [Python code map](python-code-map.md) when a public path or boundary moves.

## Verification

Run the complete repository gate and the focused tests for the affected
responsibilities before handing off a change. The gate checks Python quality,
the architecture inventory and tests, and documentation. Each failed guardrail
blocks delivery unless the maintainer explicitly waives it for that delivery.

```console
uv run --frozen --group quality python scripts/check_repository_quality.py
```

See [Platform support and testing](../developer/reference/platform-support-and-testing.md)
for the runner and test suites.

## Related documentation

- [Architecture overview](overview.md)
- [Python code map](python-code-map.md)
- [Project lifecycle contract](../developer/contracts/project-lifecycle.md)
