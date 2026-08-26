---
audience: developer
kind: plan
status: active
---

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
|   |-- mapping/       # portable rules and value semantics only
|   |-- preparation/   # portable plans and row semantics only
|   `-- execution/     # portable plan, status, and result semantics only
|-- application/
|   |-- project/
|   |-- data_version/
|   |-- workspace/
|   |   |-- mapping/
|   |   |-- preparation/
|   |   `-- execution/
|   |-- recipe/
|   |-- run/
|   `-- cutover/
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

The `mapping`, `preparation`, and `execution` domain packages do not own a
second lifecycle. They contain portable logic that can be applied to
owner-qualified state. Application use cases and persistence ports remain
grouped under the owner whose state they change.

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

### Mapping, preparation, and execution

These names describe operations that cross accepted owners. They do not create
new aggregate roots. The following placement contract applies:

| Capability | Owner of authoritative state | Portable pure logic | Allowed public application contract |
| --- | --- | --- | --- |
| Mapping | A workspace owns its working mapping draft, validation result, and current target-field evidence. An immutable Recipe revision owns only the portable mapping rules that were published from eligible work. | `domain/mapping` may define value objects, compatibility decisions, rule validation, and deterministic projections. It must not read a workspace, Data version, Recipe repository, credential, DuckDB connection, or Odoo service. | Workspace commands and queries live under `application/workspace/mapping`. Recipe compilation and publication live under `application/recipe`. Each use case receives a workspace-qualified or Recipe-qualified port, never a combined mapping repository. |
| Preparation | A Recipe-application workspace owns prepared snapshots, derived artifacts, quality inputs, and their current pointers. The Data version continues to own immutable source evidence, while the run owns only shared selection and progress meaning. | `domain/preparation` may compile and evaluate deterministic transformation plans over supplied rows and contracts. It must not open source artifacts, workspaces, worker processes, or Odoo. | Preparation use cases live under `application/workspace/preparation` and identify the workspace, pinned Recipe revision, and accepted Data version input. They depend on separate source-reader, preparation-store, and worker-gateway ports. |
| Execution | A migration run owns the shared target binding, ordering, and integrated result. Each Recipe application workspace owns its isolated execution journal and reconciliation evidence. A Recipe never owns a load result. | `domain/execution` may define load plans, status transitions, idempotency meaning, and result value objects. It must not hold credentials or call an Odoo transport, journal store, or DuckDB repository. | Run coordination lives under `application/run`; one-application execution and reconciliation live under `application/workspace/execution`. They depend on target-writer and workspace-journal ports. Only composition selects local or remote Odoo adapters. |

A pure capability module may receive identifiers as opaque values when an
algorithm must preserve lineage. It must not use those identifiers to locate
state. The owner-qualified application use case performs that lookup through a
narrow port.

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
decision changes it. Split its Python repository code by owner while keeping
the physical connection and transaction inside the DuckDB adapter.

An application coordinator must depend on an operation-oriented port such as
`FoundationCommands`, `TestRunActivationCommands`, or
`ProductionActivationCommands`. The port exposes the complete atomic command
and its owner-qualified result. It must not expose `duckdb.DuckDBPyConnection`,
a generic `connection`, `execute`, `commit`, or `rollback` method.

The DuckDB adapter may implement a private transaction-scoped repository set.
The adapter starts one transaction, passes its private transaction context to
its owner-specific repository collaborators, and commits only after every
registry mutation succeeds. No application service or domain object receives
that transaction context. A retry returns through the same public command with
the same operation identity.

The protected artifact and workspace databases cannot join the registry's
DuckDB transaction. Operations that cross those stores remain restart-safe
workflows: the registry records an operation intent, a retry verifies the
reserved meaning, and idempotent adapter steps complete missing stores without
duplicating roots.

The transaction and restart-safety baseline protects these operations:

| Operation | Required boundary | Fault and retry evidence |
| --- | --- | --- |
| Create a Project, Data version, migration run, or workspace | Each root creation, operation intent, parent revision, and registry event commits as one registry transaction. Missing owner stores are resumed idempotently. | `MigrationFoundationTests.test_fault_injection_replays_each_root_without_duplicates` |
| Publish a Recipe revision | The registry revision and reserved artifact meaning stay consistent across an artifact-store fault. A retry adds one revision only. | `ProjectAuthoringTests.test_publication_recovers_after_artifact_store_fault_and_adds_one_recipe` |
| Accept a Data version and publish a workspace source projection | Data version source evidence remains authoritative while a retry completes an interrupted cross-store projection without changing its meaning. | `DataVersionSourcePackageTests.test_freeze_and_projection_recover_after_cross_store_faults` |
| Activate an integrated Test run | The target binding, requirement plan, Recipe applications, isolated workspaces, and operation intent are one named activation command. A retry does not duplicate applications. | `IntegratedRecipeRunTests.test_same_operation_recovers_after_registry_fault_without_duplicates` |
| Qualify a Cutover plan | Registry qualification meaning and protected evidence recover under the same operation identity. | `CutoverQualificationTests.test_qualification_recovers_after_protected_evidence_fault` |
| Activate a Production run | Registry activation and workspace provisioning reuse the reserved meaning before and after the registry commit. | `ProductionRolloutTests.test_activation_recovers_after_registry_commit_before_workspace_stores` and `test_activation_retry_reuses_reserved_meaning_before_registry_commit` |

Any repository decomposition must keep these public commands and tests intact.
New atomic operations require a fault before commit, a fault after commit but
before dependent-store completion, and an exact-operation retry assertion.

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

Before reorganizing the test tree, preserve the fix for the former positional
assumption in
`test_review_projection_routes_required_default_recovery`. The full integrated
module must pass in its normal order and in the two recorded, process-isolated
orders defined by `scripts/run_seeded_unittest.py`.

Repository and adapter decompositions must also preserve bounded I/O. The
Phase 0 query and batching gates are listed in
[the deterministic baseline](../testing/code-organization-phase0-baseline.md).
Increasing one of those bounds requires an explicit performance explanation
and a reviewed replacement expectation; it must not be an incidental effect of
splitting a repository.

## Executable architecture checks

Phase 1 adds one deterministic AST-based test with no new runtime dependency.
It is explicitly transitional while flat modules remain. It should:

1. Start from a sorted module list and classify every production module by
   current layer and intended capability. The temporary ownership manifest
   must name every flat module rather than infer its owner from test order or a
   partial prefix table.
2. Resolve absolute imports, every level of relative import, imports from
   package `__init__` modules, local imports, and imports beneath
   `TYPE_CHECKING` or `typing.TYPE_CHECKING`. Type-only edges do not create a
   runtime cycle, but they still participate in dependency-direction checks.
3. Fail when any production module is unclassified. The failure must print the
   module that needs an ownership decision.
4. Reject `domain -> application`, `domain -> adapters`, and `domain -> web`
   imports.
5. Reject `application -> adapters` and `application -> web` imports.
6. Allow adapters to import the application ports they implement.
7. Restrict concrete adapter construction to composition modules and worker
   entry points.
8. Reject non-trivial runtime strongly connected module components.
9. Print the exact offending import path when it fails.

The Phase 0 inventory already resolves relative and type-only imports and
fails when an unknown nested production package appears. Phase 1 adds the
temporary complete flat-module ownership manifest and the zero-violation
dependency rules. Delete that manifest as Phase 3 moves the last flat module
to a path that communicates its owner. Do not let the transitional manifest
become the permanent organization system.

Do not create a permanent allow-list for the current application-to-adapter
imports or the inspection-worker cycle. Remove those edges first, then make
the zero-violation rule the baseline.

Add capability ownership checks after the package moves stabilize. A Data
version package should not import workspace execution or Recipe publication.
A Recipe domain package should not import concrete source, target, credential,
or execution adapters.

## Delivery sequence

### Phase 0: Establish a deterministic baseline

Phase 0 is implemented. Its maintained evidence is
[the deterministic baseline](../testing/code-organization-phase0-baseline.md).

- The integrated-run regression locates the intended Recipe application by
  identity instead of relying on card position.
- `scripts/run_seeded_unittest.py` sorts test identifiers before applying the
  recorded seeds `1729` and `20260826`. It runs each seed in a new process and
  also fixes `PYTHONHASHSEED` to that value.
- `scripts/architecture_inventory.py` deterministically records the current
  production modules, runtime and type-only imports, strongly connected
  components, and current application-to-adapter edges.
- `tests/architecture_phase0_baseline.json` is the reviewed current snapshot.
  A structural change must update it deliberately and explain the diff.
- Focused owner commands, atomic-operation tests, and bounded registry,
  workspace, Odoo, and execution I/O checks are recorded as regression gates.

**Exit condition:** The architecture fixture matches, the focused suites pass
together and independently, and the integrated suite passes in normal order
and both recorded process-isolated orders.

### Phase 1: Enforce dependency direction

Phase 1 is implemented. The preparation application service owns a narrow
columnar-transformation port, and the Odoo provenance service owns the two
protected-evidence codec ports. The local Polars and AES-GCM implementations
are constructed only by browser or worker composition.

The inspection service now supplies its pure inspector to the isolated worker.
The worker no longer imports the inspection service, so the production module
graph has no inspection-worker cycle.

`tests/test_architecture_dependency_rules.py` reads the complete temporary
flat-module ownership manifest. It rejects forbidden domain and application
imports, runtime cycles, and concrete adapter construction outside composition
or a worker entry point.

**Exit condition:** Met. There is no application-to-adapter import and no
production module cycle.

### Phase 2: Decompose composition and change hubs


Phase 2 is implemented. The completed slices give local browser composition two
capability builders and sends the lifecycle and mapping-quality routes narrow
contexts. `FoundationProjectRecords` now owns Project persistence, while
`RegistryTransactionCoordinator` owns the shared registry commit or rollback
boundary. `FoundationDataVersionRecords` now owns Data version numbering,
reads, and revision-checked writes. `FoundationMigrationRunRecords` owns run
reads, target-setup changes, and revision-checked writes;
`FoundationWorkspaceRecords` owns workspace registry reads, access lineage,
listings, and updates. Both use `RegistryTransactionCoordinator` for their
registry commit or rollback boundary. `PreparationSnapshotBindings` and
`PreparationDerivedArtifactBindings` own immutable artifact reuse and their
building-session binding transactions, while `PreparationSessionLifecycle`
owns value-free status reads and terminal cleanup. `RunPlanningOperationPayloads`
owns the durable payload that lets a planned run resume without changing its
meaning. `WorkspaceSourceProjectionRecords` owns the post-commit read-side
validation of an immutable workspace source projection, and
`PreparationCanonicalProjectionBindings` owns the atomic native-projection
binding to its verified snapshot. `FoundationSourcePackageReader` owns
hash-verified reconstruction of a Data version's immutable source package.

The slice also gives Test credential-workspace lookup, reviewed Odoo
target-evidence lookup, and pre-provisioning run review named focused use
cases. `TestRunSetupStartUseCase` owns the restart-safe creation of a Test
delivery, shared setup workspace, and durable setup binding. Public repository
and service ports remain unchanged, so callers retain the existing schema,
transaction, and restart-safety behavior.

**Exit condition:** Met. Each extracted component has one named reason to
change; public ports, schemas, transaction boundaries, bounded-I/O behavior,
and restart-safety tests remain unchanged.

### Phase 3: Move capability packages

Phase 3 is complete. The completed Project slice moves Project identity and
governance state to `domain/project/models.py`, and moves Project commands and
the consumer-owned persistence port to `application/project`. The completed
Data version slice moves data-delivery identity and accepted-state rules to
`domain/data_version/models.py`, and moves Data version commands and their port
to `application/data_version`. The completed workspace slice moves isolated
workspace identity, lifecycle, and setup state to `domain/workspace/models.py`,
and moves workspace lifecycle commands and their port to
`application/workspace`. The old flat module paths are deleted; composition,
adapters, workflow references, and focused tests now import the owner-and-layer
locations directly. The completed Recipe slice moves Recipe identity,
immutable revisions, and publication diagnostics to `domain/recipe/models.py`,
and moves Recipe reads and their port to `application/recipe`. The completed
Run slice moves run identity and lifecycle state to `domain/run/models.py`,
and moves run commands and their port to `application/run`. The completed
Cutover slice moves immutable plans, qualification evidence, and selection
contracts to `domain/cutover/models.py`; the existing focused Cutover
application services remain at their current application paths.

- Move one capability at a time with `git mv`.
- Update all imports, code-map entries, workflow references, and focused tests
  in the same change.
- Delete the old module path in the same change. Do not leave import aliases.
- Start with the accepted ownership roots: Project, Data version, workspace,
  Recipe, run, and cutover.
- Move mapping, preparation, and execution after the ownership roots provide
  stable destinations.

**Exit condition:** Met. Each moved root communicates its owner and layer, the
old flat root paths are absent, and the architecture test enforces the target
dependency matrix.

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
- the Phase 0 registry statement counts, workspace-open counts, shared Odoo
  capture counts, and bounded execution and reconciliation requests;
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
