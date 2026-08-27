---
audience: developer
kind: architecture
status: current
---

# Code organization

## Purpose

This guide is the current source of truth for placing and reviewing Impodo
code. It helps a human maintainer or coding agent find the owner of state,
choose the correct dependency direction, and select focused tests before
changing behavior.

Impodo organizes Python code by layer first and by business capability second.
The layer says what kind of work a module performs. The capability says which
business owner or workflow gives the module a reason to change.

## Begin with the owner

The filesystem reinforces these separate lifecycles:

```text
MigrationProject
|-- DataVersion 1..many: complete source deliveries and source evidence
|-- MigrationRun 1..many: one target use and shared run evidence
|   `-- RecipeApplication 0..many
|       `-- MigrationWorkspace: isolated current working evidence
|-- Recipe 0..many: immutable reusable rule revisions
`-- CutoverPlan: integrated qualification and rollout-selection meaning
```

Containment does not merge these owners. A Project identifies and governs its
contained roots, but it does not absorb their evidence. A cross-owner action
belongs in an application coordinator and uses an owner-qualified port.

| Owner | Authoritative state | Primary domain path | Primary application path |
| --- | --- | --- | --- |
| Project | Identity, status, membership, and Project-level governance. | `domain/project` | `application/project`; Project authoring coordination remains in `application/migration_project_authoring_service.py`. |
| Data version | Source files, detected catalogues, confirmed parsing choices, logical datasets, snapshots, and protected source references. | `domain/data_version` | `application/data_version`; workspace publication of selected source references uses the named cross-owner services at the application root. |
| Migration workspace | Owner lineage, selected Data version references, and current mapping, preparation, comparison, execution, and reconciliation evidence. | `domain/workspace` plus the portable operational packages described below. | `application/workspace` and its `mapping`, `preparation`, and `execution` packages. |
| Recipe | Immutable portable rules and revision lineage. A Recipe owns no source rows, credentials, approvals, writes, or migration results. | `domain/recipe` plus the exact envelope and application contracts in `domain/recipe_envelope.py` and `domain/recipe_applications.py`. | `application/recipe`; compilation, publication, and fresh application retain named application-layer facades. |
| Migration run | Target binding, requirement union, application ordering, shared run status, and integrated result meaning. | `domain/run` | `application/run` |
| Cutover plan | Versioned dependencies, qualification meaning, write ownership, controls, and rollout selection. | `domain/cutover` | `application/cutover_plan_service.py` and `application/production_cutover_service.py` |

The registry owns the `MigrationWorkspace` identity and its `DRAFT` or `READY`
lifecycle. `domain/workspace/workbench.py` is a contained engine projection. It
must not acquire a second workspace identity or become authoritative beside
the registry.

## Implemented package shape

The current production tree is:

```text
src/impodo/
|-- domain/
|   |-- project/       data_version/    workspace/
|   |-- recipe/        run/             cutover/
|   |-- mapping/       preparation/     execution/
|   |-- compiler/      staging/         preflight/
|   |-- odoo/          schema/          shared/
|   `-- cohesive domain contracts that span one named operation
|-- application/
|   |-- project/       data_version/    recipe/    run/
|   |-- workspace/
|   |   |-- mapping/
|   |   |-- preparation/
|   |   `-- execution/
|   |-- shared/
|   `-- named cross-owner coordinators and stable facades
|-- adapters/
|   |-- duckdb/
|   |   `-- schema/
|   |-- artifacts/
|   |-- jobs/
|   |-- odoo/
|   |-- protected_evidence/
|   `-- focused integration facades
`-- web/
    |-- routers/
    |-- presenters/
    |-- composition/
    |-- templates/
    |   `-- mapping/
    |-- static/
    `-- application assembly and shared browser delivery
```

This implemented tree deliberately refines the provisional structure used
during remediation:

- Domain and application code use owner or capability packages whenever the
  ownership boundary is stable.
- DuckDB adapters remain grouped by storage technology. Their filenames and
  internal collaborators identify the owner while one private transaction
  coordinator protects the shared registry.
- Browser code is grouped by delivery role: routers accept requests,
  presenters construct views, and composition selects concrete adapters and
  process runtimes.
- A direct module below `domain`, `application`, `adapters`, or `web` is
  acceptable only when it is a cohesive shared contract, a named cross-owner
  coordinator, or a stable facade. New code should prefer an existing
  capability package. A new root-level exception needs an explicit ownership
  explanation during review.
- No production module may be added directly below `src/impodo`. The package
  root contains only `__init__.py` and the `python -m impodo` entry point.

## Mapping, preparation, and execution

Mapping, preparation, and execution cross accepted owners, but they do not
create additional aggregate roots. Each capability has three separate homes:
the owner of current state, portable pure logic, and an owner-qualified public
application contract.

| Capability | Owner of state | Portable pure logic | Public application contract and external implementation |
| --- | --- | --- | --- |
| Mapping | A workspace owns its draft, validation result, target-field evidence, and current decisions. A published Recipe revision owns only portable rules. | `domain/mapping` defines rule values, validation, canonicalization, and deterministic projections. `domain/compiler` compiles supplied contracts without locating owner state. | Workspace commands and queries live in `application/workspace/mapping`. Recipe compilation and publication use application-layer Recipe services. DuckDB mapping repositories implement the workspace-qualified ports. |
| Preparation | A Recipe-application workspace owns prepared snapshots, derived artifacts, quality evidence, normalization evidence, and current pointers. The Data version continues to own immutable source evidence. The run owns shared progress meaning only. | `domain/preparation` and `domain/staging` evaluate supplied transformation, quality, and staging contracts. They do not open stores or start workers. | `application/workspace/preparation` owns use cases and consumer ports. `web/composition/preparation_job_manager.py` and `preparation_worker.py` own the local process runtime. Focused DuckDB preparation collaborators implement persistence behind `PreparationSessionRepository`. |
| Execution | A run owns the target binding, application order, and integrated status. Each application workspace owns its execution journal and reconciliation evidence. A Recipe never owns a load result. | `domain/execution` defines plans, target scopes, status, write requests, and read-back meaning. `domain/run` defines shared run ordering and requirements. | Run-wide coordination lives in `application/run`. One-application loading and reconciliation live in `application/workspace/execution`. DuckDB stores the journal, `adapters/odoo` implements target ports, and `web/composition/target_readers.py` or `target_writers.py` selects the concrete adapter. |

Pure capability functions may carry owner identifiers to preserve lineage.
They must not use an identifier to find a store, credential, or external
service. The owner-qualified application use case performs that lookup through
a narrow port.

## Dependency direction

Dependencies point inward toward portable meaning:

```text
web routers and presenters ---> application use cases ---> domain
web composition -------------> application use cases
web composition -------------> adapters
adapters ---------------------> application ports and domain contracts
domain -----------------------> shared domain primitives only
```

| Layer | May depend on | Must not depend on |
| --- | --- | --- |
| `domain` | Standard library, reviewed pure libraries, and other domain contracts. | `application`, `adapters`, `web`, FastAPI, DuckDB, filesystem paths, credential stores, or job runtimes. |
| `application` | Domain contracts and consumer-owned application ports. | Concrete adapters, `web`, FastAPI, DuckDB connections, concrete filesystem stores, or concrete credential stores. |
| `adapters` | Domain contracts and the application ports that the adapter implements. | Browser routes, templates, or mutable browser context. |
| `web` routers and presenters | Application use cases, read projections, and presentation contracts. | Direct DuckDB repository, protected-store, credential-vault, or Odoo-adapter construction. |
| `web/composition` and approved entry points | Application services, concrete adapters, and runtime configuration. | Business decisions that belong in a domain function or application use case. |

`tests/architecture/test_dependency_rules.py` enforces the forbidden layer
edges, rejects runtime dependency cycles, and restricts concrete adapter
construction to approved composition and worker entry points.
`tests/architecture/test_inventory.py` also rejects an unclassified production
package and compares the complete deterministic import graph with its reviewed
snapshot.

## Ports and transaction boundaries

The application layer owns ports from the consumer's perspective. A port names
the business operation or evidence that its use case needs. It does not expose
a technology-neutral copy of every repository method.

When one registry action changes several owners, the public port exposes one
named atomic command. The DuckDB adapter opens the connection, begins the
transaction, gives a private transaction context to owner-focused
collaborators, and commits or rolls back. An application service never receives
`DuckDBPyConnection`, `execute`, `commit`, `rollback`, or a generic connection
object.

`adapters/duckdb/migration_foundation_repository.py` is the stable registry
facade. Its Project, Data version, run, workspace, operation-intent, and source
package collaborators share the private
`adapters/duckdb/registry_transaction.py` boundary. Splitting an adapter must
not split an atomic operation.

The registry, protected evidence, Data version database, and workspace
databases cannot share one DuckDB transaction. A cross-store workflow records
an operation intent, reserves exact meaning, completes idempotent store steps,
and returns the same result when retried with the same operation identity.
The maintained fault and retry commands are listed in the
[code-organization regression baseline](../testing/code-organization-phase0-baseline.md#atomic-operation-gates).

Repository decomposition must also preserve bounded I/O. A narrower port is
not an excuse to add one registry query, workspace open, Odoo request, write,
or read-back call per Project, Recipe, field, relationship, or source row. The
[bounded-I/O gates](../testing/code-organization-phase0-baseline.md#bounded-io-gates)
protect the accepted query and batching limits.

## Browser files and assets

Browser delivery follows these boundaries:

- `web/routers` validates transport input and calls application use cases.
- `web/presenters` turns application results into explicit view data.
- `web/composition` constructs concrete stores, Odoo adapters, job runtimes,
  and command-line entry points.
- `web/templates` contains server-rendered pages and shared fragments. The
  Mapping entry page composes named partials below `templates/mapping`.
- `web/static/app.js` contains only shared browser behavior. Page workflows
  load focused scripts by name. Shared CSS is split into tokens, layout,
  components, and workflow-page rules; Mapping and target connection retain
  focused styles.

A route context should expose only the capability required by that router. A
page-specific control belongs with its page asset unless several pages depend
on the same behavior and contract.

## Test organization

Every discovered test names its evidence level before its capability:

```text
tests/
|-- architecture/
|-- domain/<capability>/
|-- application/<owner-or-capability>/
|-- integration/<external-boundary>/
|-- e2e/
|-- performance/
`-- support/
```

Domain tests prove portable decisions. Application tests prove owner-qualified
use cases. Integration tests prove DuckDB, artifact, protected-evidence, Odoo,
and browser boundaries. End-to-end tests retain only complete journeys, and
performance tests protect explicit scale behavior.

`tests/support` contains non-discovered builders and probes. Builders require
explicit Project, Data version, run, Recipe, application, and workspace
identities. Moved tests use `tests.support.paths.REPOSITORY_ROOT`; they do not
infer the repository root from package depth or depend on process-global test
state.

## Placement procedure

Before adding or moving a module:

1. Name the owner of the state or evidence that the behavior reads or changes.
2. Decide whether the code is portable meaning, application coordination, an
   external implementation, browser delivery, or runtime construction.
3. Use the existing owner or capability package in that layer. Treat a new
   direct layer module as an exception that needs a cohesive cross-owner or
   stable-facade reason.
4. Give cross-owner sequencing to one application coordinator. Keep each
   owner-specific read or command behind a narrow port.
5. Keep DuckDB, filesystem, encryption, credential, Odoo, and worker details
   in adapters or composition.
6. Identify the focused domain or application test and the relevant adapter or
   browser contract before editing.
7. Update the [Python code map](python-code-map.md) when a public path, facade,
   trace, or composition boundary changes.

Do not copy Data version source evidence into a workspace or Recipe. Do not
copy Test workspace evidence, credentials, approvals, or outcomes into
Production.

## Review triggers

Review ownership and cohesion when any of these conditions appears:

- One file changes for more than one business capability.
- One application service coordinates unrelated operator actions.
- One concrete repository exposes several unrelated owner-specific ports.
- A route context gives most routes services that they do not use.
- A test needs several unrelated lifecycle phases before it can make one
  assertion.
- A new import crosses a layer or creates a cycle.
- A workbench cache starts to look authoritative beside a Data version, run,
  workspace, or Project owner.
- A repository split increases a query, workspace-open, or Odoo-call bound.

The reviewer should split the responsibility or record why one transaction,
algorithm, immutable contract, or stable public facade requires it to remain
together.

## Verification

Run the structural gates after a package, port, composition, test, template, or
asset change:

```console
python scripts/architecture_inventory.py \
  --check tests/architecture/phase0_baseline.json
python -m unittest \
  tests.architecture.test_inventory \
  tests.architecture.test_dependency_rules \
  tests.architecture.test_test_organization \
  tests.architecture.test_static_asset_ownership
python scripts/documentation_quality.py --check
python scripts/code_documentation_inventory.py --check
git diff --check
```

Also run the focused owner, transaction, bounded-I/O, browser, seeded-order, or
end-to-end commands that match the change. The
[regression baseline](../testing/code-organization-phase0-baseline.md) records
those commands and their exact preservation limits.

## Related documentation

- [Architecture overview](overview.md)
- [Python code map](python-code-map.md)
- [Code-organization regression baseline](../testing/code-organization-phase0-baseline.md)
- [Completed remediation delivery record](../plans/code-organization-remediation.md)
- [Architecture decisions](../decisions/README.md)
- [Project lifecycle contract](../developer/contracts/project-lifecycle.md)
