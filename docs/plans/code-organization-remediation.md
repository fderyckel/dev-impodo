---
audience: developer
kind: plan
status: proposed
---



I would make these refinements before treating it as the execution contract:
1. Clarify the home of cross-cutting mapping, preparation, and execution code. The proposed tree lists them as separate domain capabilities, while the placement rules also describe much of their evidence as workspace-owned. Define, for each, the owner of its state, its portable pure logic, and the allowed public contract. Otherwise the new tree can recreate the same ambiguity it aims to remove. See [target package shape (line 73)](dev-impodo\docs\plans\code-organization-remediation.md:73) and [workspace placement (line 144)](\dev-impodo\docs\plans\code-organization-remediation.md:144).
2. Specify the transaction boundary as a narrow port or transaction-scoped repository interface—not a DuckDB connection passed through application services. Enumerate the atomic operations and fault/retry tests that protect them. The principle is right; the interface needs to prevent persistence details leaking inward. See [cross-owner transactions (line 181)](\dev-impodo\docs\plans\code-organization-remediation.md:181).
3. Make the Phase 1 architecture check explicitly transitional. It must deterministically classify existing flat modules, resolve relative and type-only imports correctly, and fail on unclassified production code. This avoids replacing one hidden organization system with a fragile test-side mapping. See [executable architecture checks (line 332)](\dev-impodo\docs\plans\code-organization-remediation.md:332).
4. Make the order-dependence proof reproducible: use recorded fixed shuffle seeds or process-isolated cases, rather than one nondeterministic shuffled run. See [Phase 0 (line 357)](\dev-impodo\docs\plans\code-organization-remediation.md:357).
5. Add explicit batching and query-count preservation checks. Splitting broad repositories into narrow ports can accidentally introduce N+1 registry queries, workspace opens, or Odoo reads. The existing architecture requires bounded shared Odoo capture, so this deserves a regression gate alongside restart-safety tests.

# Code organization remediation plan

## Purpose

This plan reorganizes Impodo so that the filesystem, dependency graph, tests,
and developer guidance reinforce the accepted Project, Data version,
workspace, Recipe, and migration-run ownership model.

The plan changes code organization. It does not change product behavior,
domain ownership, browser decisions, persistence generations, hashes, or Odoo
authority.

The intended reader is a human maintainer or coding agent preparing a focused
refactor or deciding where new code belongs.

## Desired result

After this plan is complete, a maintainer should be able to answer these
questions from a file path before opening the file:

1. Which business capability owns the behavior?
2. Is the file domain meaning, application coordination, an outbound adapter,
   browser delivery, or a test?
3. Which dependencies may it import?
4. Which contract and focused test protect the change?

The accepted domain model remains:

```text
MigrationProject
|-- DataVersion 1..many: complete source deliveries and source evidence
|-- MigrationRun 1..many: one target use and shared run evidence
|   `-- RecipeApplication 0..many
|       `-- MigrationWorkspace: isolated current working evidence
|-- Recipe 0..many: immutable reusable rule revisions
`-- CutoverPlan: integrated qualification and rollout selection meaning
```

Containment does not merge these owners. Cross-owner behavior belongs in an
application coordinator, not in a larger combined domain aggregate.

## Target dependency rule

The target dependency direction is:

```text
browser routes and presenters ---> application use cases ---> domain
local composition --------------> application use cases
local composition --------------> adapters
adapters ------------------------> application ports and domain contracts
domain --------------------------> shared domain primitives only
```

The detailed rules are:

| From | May depend on | Must not depend on |
| --- | --- | --- |
| `domain` | Standard library, reviewed pure libraries, and shared domain primitives | `application`, `adapters`, `web`, FastAPI, DuckDB, filesystem paths, credential stores, or job runtimes |
| `application` | Domain contracts and consumer-owned ports | Concrete adapters, `web`, FastAPI, DuckDB connections, concrete filesystem stores, or concrete credential stores |
| `adapters` | Domain contracts and the application ports they implement | `web` routes, templates, or mutable browser context |
| `web` routes and presenters | Application use cases, read projections, and presentation contracts | Concrete DuckDB repositories, protected stores, or Odoo adapter construction |
| `web/composition` | Application services, adapters, and runtime configuration | Business decisions that belong in an application use case |

An adapter importing an application port is correct. An application service
importing a concrete adapter is not.

## Target package shape

Impodo should keep a layer-first structure because ADR-008 requires portable
domain and application layers. Each layer should then group files by the
business capability that gives them a reason to change.

```text
src/impodo/
|-- domain/
|   |-- shared/
|   |-- project/
|   |-- data_version/
|   |-- workspace/
|   |-- recipe/
|   |-- run/
|   |-- cutover/
|   |-- mapping/
|   |-- preparation/
|   `-- execution/
|-- application/
|   |-- project/
|   |-- data_version/
|   |-- workspace/
|   |-- recipe/
|   |-- run/
|   |-- cutover/
|   |-- preparation/
|   `-- execution/
|-- adapters/
|   |-- duckdb/
|   |   |-- registry/
|   |   |-- data_version/
|   |   |-- workspace/
|   |   `-- run/
|   |-- artifacts/
|   |-- odoo/
|   `-- protected_evidence/
`-- web/
    |-- composition/
    |-- projects/
    |-- workspaces/
    |-- runs/
    `-- shared/
```

This is a destination, not a request for one large move. Current import paths
should change in reviewed capability slices. The final structure must not keep
old import modules as runtime aliases.

## Ownership and placement rules

### Project

Place Project identity, status, Project-level governance, and membership
lineage contracts under `domain/project`. Place new-Project coordination and
Project commands under `application/project`.

A Project module may identify contained roots. It must not absorb Data version
source evidence, workspace working evidence, Recipe payloads, or run results.

### Data version

Place source-package membership, source files, detected catalogues, confirmed
parsing choices, logical datasets, immutable source snapshots, and source
artifact contracts under `domain/data_version`.

Place source intake and acceptance use cases under `application/data_version`.
A Data version use case may coordinate with a workspace to publish selected
references. It must not store Recipe rules, target credentials, mapping
results, or execution results in the Data version.

### Workspace

Place `MigrationWorkspace`, workspace access lineage, bounded source
projections, and current mapping, preparation, comparison, execution, and
reconciliation evidence under the appropriate workspace or operational domain
packages.

The current flat `WorkspaceState` is a contained workbench projection. Its
eventual path and name must make that status explicit, such as
`domain/workspace/workbench.py`. It must not become a second workspace
identity or lifecycle owner.

### Recipe

Place portable Recipe meaning, envelope validation, immutable revisions, and
Recipe application contracts under `domain/recipe`. Place compilation,
publication, and fresh application use cases under `application/recipe`.

A Recipe module must not depend on physical files, current Data version IDs,
credentials, numeric Odoo IDs, approvals, journals, or reconciliation results.

### Migration run

Place run identity, target binding, union requirement meaning, Test setup
binding, Production binding, and shared status projections under `domain/run`.
Place Test setup, integrated planning, Production activation, required-default
recovery, and run review under focused use-case modules in `application/run`.

Run coordination may select exact Recipe revisions and create application
workspaces. It must not make those workspaces share mutable working evidence.

### Cutover

Place Cutover plan revisions, qualification evidence contracts, dependency
meaning, and rollout selection under `domain/cutover`. Place qualification and
selection coordination under `application/cutover`.

### Cross-owner transactions

Keep `registry.duckdb` as one local registry unless a separate architecture
decision changes it. Split its Python repository code by owner while sharing
one explicit connection and transaction abstraction.

An operation that atomically creates or changes several roots belongs in a
named transaction coordinator. Owner-specific repositories should accept or
reuse that transaction. They must not open independent transactions that make
the operation appear atomic when it is not.

## Repository decomposition

### Project registry

Decompose `MigrationFoundationRepository` into internal collaborators with
these responsibilities:

- Project identity and summary persistence;
- Data version identity and source-package registry persistence;
- migration-run and target-setup persistence;
- MigrationWorkspace and source-projection persistence;
- workspace access and bounded lineage queries;
- operation-intent and audit persistence; and
- a registry transaction coordinator for restart-safe multi-root operations.

The collaborators may share one registry database object and serialization
helpers. Application services should receive only the narrow port they use.

### Run planning

Separate run-planning persistence into:

- activation and restart-safe provisioning commands;
- target, requirement, and reference projections;
- Recipe application state and issue persistence; and
- bounded run progress queries.

Keep the exact activation transaction and validation order unchanged.

### Preparation sessions

Separate the current preparation-session adapter into internal components for:

- session lifecycle;
- prepared-snapshot and derived-artifact bindings;
- direct-row and native-projection writing;
- relationship and quality indexing;
- normalization materialization;
- stored-run reading and hashing; and
- failure cleanup.

The public application ports may remain stable while the adapter delegates to
these components. Splitting a file must not split a transaction that protects
one publication.

## Application service decomposition

Split services by named operator or recovery actions, not by arbitrary line
counts.

`TestRunSetupService` should become focused use cases for:

- starting Test setup;
- presenting and saving fresh-data requirements and run values;
- matching physical tables to Recipe inputs;
- presenting run-owned Odoo requirements; and
- resolving the setup workspace that owns credentials.

`MigrationRunPlanningService` should become focused use cases for:

- reviewing a Test run;
- activating a Test run;
- reviewing and activating a Production run;
- materializing isolated Recipe applications;
- reviewing and confirming required Odoo defaults; and
- recovering eligible blocked Test applications.

Shared requirement union, dependency ordering, and write-collision logic
should be pure domain functions or focused domain services. They should not be
duplicated across Test and Production coordinators.

## Composition and browser organization

Keep `create_local_app` as the public local entry point. Move construction into
small builders that return narrow capability contexts:

- registry and evidence stores;
- Project and Data version services;
- workspace authoring services;
- Recipe publication and application services;
- Test and Production run services;
- execution and reconciliation services;
- target readers and writers; and
- background jobs.

Project routers should receive a Project context. Workspace routers should
receive a workspace-stage context. Run routers should receive a run context.
No route context should expose a service solely because another router needs
it.

Split `app.js` into small framework-free modules for shared navigation,
Mapping, target connection, transformation editing, and job polling. Split
`app.css` into tokens, layout, reusable components, and page-specific files.
Load them through explicit static tags or one small entry point. Do not add a
frontend build system unless a separate need justifies it.

Split `workspace_mapping.html` into named partials or macros around stable
forms and dialogs. Preserve exact input names, URLs, progressive enhancement,
and server-rendered recovery behavior.

## Test organization

Use a test hierarchy that reveals both capability and evidence level:

```text
tests/
|-- architecture/
|-- domain/
|   |-- data_version/
|   |-- workspace/
|   |-- recipe/
|   `-- run/
|-- application/
|   |-- data_version/
|   |-- recipe/
|   `-- run/
|-- integration/
|   |-- duckdb/
|   |-- artifacts/
|   `-- web/
|-- e2e/
`-- support/
```

The hierarchy may use fewer levels when a folder would contain only one file.
The important rule is that a test's path identifies what it proves.

Extract deterministic Project, Data version, run, workspace, Recipe, and
target builders into `tests/support`. Builders must require explicit owner
identities instead of relying on process globals or prior tests.

Retain focused contract tests for each owner and transaction. Keep one small
end-to-end browser smoke path for the complete journey. Split the current
1,911-line browser scenario into capability tests that share reviewed setup
helpers, not shared mutable application state.

Before reorganizing the test tree, fix the current order-dependent
`test_review_projection_routes_required_default_recovery` failure. The full
integrated module must pass in its normal order and in a shuffled order.

## Executable architecture checks

Add one deterministic AST-based test with no new runtime dependency. It should:

1. classify every production module by layer and capability;
2. reject `domain -> application`, `domain -> adapters`, and `domain -> web`
   imports;
3. reject `application -> adapters` and `application -> web` imports;
4. allow adapters to import the application ports they implement;
5. restrict concrete adapter construction to composition modules and worker
   entry points;
6. reject non-trivial strongly connected module components; and
7. print the exact offending import path when it fails.

Do not create a permanent allow-list for the current application-to-adapter
imports or the inspection-worker cycle. Remove those edges first, then make
the zero-violation rule the baseline.

Add capability ownership checks after the package moves stabilize. A Data
version package should not import workspace execution or Recipe publication.
A Recipe domain package should not import concrete source, target, credential,
or execution adapters.

## Delivery sequence

### Phase 0: Establish a deterministic baseline

- Fix the integrated-test order dependence.
- Record the normal focused commands for Project, Data version, workspace,
  Recipe, run, and cutover behavior.
- Capture the current import graph and module-cycle result as test fixtures or
  deterministic generated expectations.

**Exit condition:** The focused suites pass together, independently, and in a
different order.

### Phase 1: Enforce dependency direction

- Define consumer-owned ports for Polars preparation and protected evidence
  codecs.
- Inject their local implementations from composition and worker entry points.
- Break the `inspection.py` and `source_worker.py` cycle by extracting shared
  contracts and the pure inspector or by injecting the isolated worker.
- Add the architecture import and cycle test.

**Exit condition:** There is no application-to-adapter import and no production
module cycle.

### Phase 2: Decompose composition and change hubs

- Split local composition into capability builders and narrow route contexts.
- Decompose `MigrationFoundationRepository` behind the current ports and one
  shared transaction coordinator.
- Decompose run-planning and preparation-session persistence without changing
  schema or transaction meaning.
- Split Test setup and run planning into focused use cases.

**Exit condition:** Each extracted component has one named reason to change,
and existing behavior and restart-safety tests remain unchanged.

### Phase 3: Move capability packages

- Move one capability at a time with `git mv`.
- Update all imports, code-map entries, workflow references, and focused tests
  in the same change.
- Delete the old module path in the same change. Do not leave import aliases.
- Start with the accepted ownership roots: Project, Data version, workspace,
  Recipe, run, and cutover.
- Move mapping, preparation, and execution after the ownership roots provide
  stable destinations.

**Exit condition:** The file path communicates both the layer and the owner,
and the architecture test enforces the target dependency matrix.

### Phase 4: Organize tests and browser assets

- Move tests into the capability and evidence-level hierarchy.
- Replace broad mutable fixtures with explicit builders.
- Split the Mapping template, JavaScript, and CSS by page or component.
- Add focused JavaScript tests only where client behavior cannot be proved by
  server-rendered tests and static contract assertions.

**Exit condition:** A focused change has a focused test command and does not
require editing a cross-feature browser asset unless the shared behavior truly
changes.

### Phase 5: Promote the final organization to current architecture

- Update the architecture overview and Python code map.
- Add a current `docs/architecture/code-organization.md` based on the final,
  implemented package tree.
- Update this plan to completed or replace it with a dated delivery report.

**Exit condition:** Current documentation describes only the implemented
structure, and this proposed plan is no longer needed as current guidance.

## Change protocol for maintainers and agents

Before changing code:

1. Name the business owner of the state or evidence.
2. Name the application action that changes or reads it.
3. Read the owning lifecycle contract and workflow registration.
4. Confirm that the proposed dependency points inward.
5. Identify the narrow focused tests before editing.

While changing code:

1. Put domain invariants with the owner, not in a router or adapter.
2. Put cross-owner sequencing in an application coordinator.
3. Put DuckDB, filesystem, credential, Odoo, encryption, and worker details in
   adapters or composition.
4. Pass owner-specific ports instead of a broad repository or full web
   context when practical.
5. Preserve exact identity, hash, authorization, idempotency, concurrency, and
   transaction boundaries.
6. Do not copy Data version source data into a workspace or Recipe.
7. Do not copy Test evidence or credentials into Production.

Before completing a change:

1. Run the architecture dependency test.
2. Run the owning domain and application tests.
3. Run the relevant persistence or browser contract tests.
4. Run documentation and code-orientation checks.
5. Update the code map when a public location or trace changes.
6. State any broader suite, browser, performance, or Odoo verification that was
   not run.

## Review triggers

These are review prompts, not automatic line-count failures:

- A file changes for more than one business capability.
- An application service coordinates several unrelated operator actions.
- One concrete repository implements several owner-specific ports.
- A route context exposes a service unused by most routes in that group.
- A test method needs several unrelated lifecycle phases to establish its
  assertion.
- A new import crosses a layer or creates a module cycle.
- A workbench cache begins to look authoritative beside a Data version, run,
  or Project owner.

When a trigger occurs, the reviewer should either split the responsibility or
record why one transaction, algorithm, or immutable contract requires it to
remain together.

## Verification requirements

Every remediation slice must preserve:

- the Project and workspace lifecycle tests;
- Data version source-package and artifact-root tests;
- Recipe portability and publication tests;
- integrated Test run and Production rollout tests;
- workspace authorization and identity-confusion tests;
- storage generation and forward-upgrade tests;
- restart-safe operation tests;
- documentation quality and code-orientation checks; and
- `git diff --check`.

A package move alone does not require a screenshot. A router, template, label,
control, or browser decision change must follow the paired user and developer
workflow documentation process and refresh the affected screenshot when the
decision point changes.

## Non-goals

- This plan does not introduce a new feature.
- It does not replace DuckDB or add the hosted deployment.
- It does not change the current persistence layout or schema generations.
- It does not merge Project, Data version, run, workspace, Recipe, or Cutover
  plan identities.
- It does not create a new frontend framework or require a bundler.
- It does not enforce a docstring or line-count quota.
- It does not retain old Python imports through compatibility aliases.

## Related evidence

- [Code architecture maintainability audit](../reports/code-architecture-maintainability-audit-2026-08-26.md)
- [Architecture overview](../architecture/overview.md)
- [Python code map](../architecture/python-code-map.md)
- [ADR-008 and ADR-014](../decisions/README.md)
- [Project and workspace lifecycle](../developer/contracts/project-lifecycle.md)
- [Integrated Test run lifecycle](../developer/contracts/integrated-run-lifecycle.md)
