---
audience: developer
kind: plan
status: completed
---

# Code organization remediation delivery record

## Purpose

This completed plan records how Impodo reorganized the filesystem, dependency
graph, tests, and developer guidance around the accepted Project, Data version,
workspace, Recipe, and migration run ownership model.

The [current code-organization guide](../architecture/code-organization.md)
supersedes the prescriptive sections below. This file preserves the execution
contract, design rationale, phase outcomes, and verification evidence for the
completed work.

The delivery changed code organization. It did not change product behavior,
domain ownership, browser decisions, persistence generations, hashes, or Odoo
authority.

The intended reader is a human maintainer or coding agent reviewing why the
reorganization was performed and which evidence qualified its delivery.

## Delivered result

A maintainer can now answer these questions from a file path before opening the
file:

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

## Provisional target package shape

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

This tree guided the reviewed capability slices. The implemented structure
refined it by keeping adapters grouped by external technology and browser code
grouped by delivery role. The current guide records that final structure. No
old import module remains as a runtime alias.

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

Split `mapping/page.html` into named partials or macros around stable
forms and dialogs. Preserve exact input names, URLs, progressive enhancement,
and server-rendered recovery behavior.

## Test organization

Use a test hierarchy that reveals both capability and evidence level:

```text
tests/
|-- architecture/
|-- domain/
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
|   |-- run/
|   `-- cutover/
|-- integration/
|   |-- duckdb/
|   |-- artifacts/
|   |-- odoo/
|   |-- protected_evidence/
|   `-- web/
|-- e2e/
|-- performance/
`-- support/
```

The hierarchy may use fewer levels when a folder would contain only one file.
The important rule is that a test's path identifies what it proves.

Extract deterministic Project, Data version, run, workspace, Recipe, and
target builders into `tests/support`. Builders must require explicit owner
identities instead of relying on process globals or prior tests. Filesystem
access from moved tests uses `tests.support.paths.REPOSITORY_ROOT`; a test must
not infer the repository root from its current package depth.

Retain focused contract tests for each owner and transaction. Keep one
end-to-end browser smoke path for the complete journey. Split the former
8,718-line browser module into capability tests that share reviewed setup
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
- `tests/architecture/phase0_baseline.json` is the reviewed current snapshot.
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

`tests/architecture/test_dependency_rules.py` reads the complete temporary
flat-module ownership manifest. It rejects forbidden domain and application
imports, runtime cycles, and concrete adapter construction outside composition
or a worker entry point.

**Exit condition:** Met. There is no application-to-adapter import and no
production module cycle.

### Phase 2: Decompose composition and change hubs

Phase 2 is complete. The completed slices give local browser composition two
capability builders and send the lifecycle and mapping-quality routes narrow
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

`application/run/fresh_data_matching.py` now owns the deterministic matching
of selected Recipe inputs to detected tables in one fresh Test delivery.
`TestRunSetupService` retains its public method but delegates that pure decision
instead of combining it with persistence and authorization coordination.
`application/run/fresh_data_values.py` now owns Recipe source and parameter
projection, shared run-value merging, automatic export-date normalization, and
saved-value ownership checks. `TestRunSetupService` retains persistence and
authorization coordination while calling those portable functions.

`application/run/odoo_requirements.py` now owns the authorized read-only query
that projects the exact selected Recipe revisions into one combined Odoo model,
field, and supporting-relationship scope. It receives narrow selection and
Recipe-revision readers, performs one bulk Recipe read, and never contacts
Odoo. `TestRunSetupService` retains its browser-facing method and delegates the
query. `tests/application/run/test_odoo_requirements.py` proves the bounded
read and fail-closed semantic-hash behavior without building a browser app.

`domain/run/planning.py` now owns deterministic dependency ordering, Odoo
requirement unioning, reference-version and write-ownership collision checks,
and the reusable requirement hash. `RunReviewUseCase` calls those domain
functions directly instead of receiving seven callbacks from
`MigrationRunPlanningService`.

`TestRunFreshDataUseCase` now owns the bounded Recipe read, fresh-data run-value
plan and save, physical-table matching, and activation parameter reconstruction.
`TestRunSetupStartUseCase` owns restart-safe setup creation, while
`TestRunOdooRequirementsUseCase` owns the run-qualified Odoo requirement query.
`TestRunSetupService` remains only as the stable setup capability facade and
coordinates the final activation call.

`MigrationRunPlanningService` is now a stable facade over `RunReviewUseCase`,
`TestRunActivationUseCase`, `ProductionRunReviewUseCase`,
`ProductionRunActivationUseCase`, `RunApplicationMaterializer`,
`RunApplicationRecoveryUseCase`, and `RunTargetEvidenceUseCase`. Test and
Production authority checks and restart paths no longer share one method body.
The application layer still receives operation-oriented repositories and never
receives a DuckDB connection.

`MigrationFoundationRepository` now assembles owner-focused adapter components.
Project, Data version, run, and workspace records and create commands are
separate. Operation intents, source-package writes, registry identity support,
and record codecs are also separate. They retain the single private
`RegistryTransactionCoordinator`; the public repository port and exact
operation identities are unchanged.

`PreparationSessionRepository` now assembles separate direct/native writers,
quality and relationship indexes, normalization records, stored-run readers,
and failure cleanup. Snapshot, derived-artifact, canonical-projection, and
lifecycle bindings remain focused collaborators. One publication transaction
still protects each session, and the public preparation port is unchanged.

**Exit condition:** Met. The four former change hubs are stable facades or one
cohesive operation family. Public ports, schemas, transaction boundaries,
bounded-I/O assertions, deterministic test orders, and restart-safety tests
remain unchanged and green.

### Phase 3: Move capability packages

Phase 3 is complete. Project, Data version, workspace, Recipe, run, and
Cutover identities and contracts now live below their `domain` owners. Their
commands, queries, and consumer-owned ports live below `application`. The
workspace Mapping, Preparation, and Execution use cases remain explicitly
workspace-qualified; run-wide coordination remains below `application/run`.

The cross-cutting capabilities now expose unambiguous seams:

- workspace-owned Mapping evidence uses `domain/workspace` contracts,
  portable decisions use `domain/mapping`, and use cases use
  `application/workspace/mapping`;
- workspace-owned preparation evidence uses `domain/workspace` and
  `domain/preparation` contracts, while orchestration uses
  `application/workspace/preparation` and process construction uses
  `web/composition`; and
- run meaning uses `domain/run`, workspace journals and target ports use
  `domain/execution`, run coordination uses `application/run`, isolated load
  and reconciliation use `application/workspace/execution`, and Odoo
  implementations use `adapters/odoo`.

Mixed flat modules were split rather than hidden under a new directory.
Artifact and secret-store ports now live in `application/shared`; their local
filesystem, keyring, and in-memory implementations live under `adapters`.
Transport-neutral Odoo requests and snapshots live in `domain/odoo`, while
JSON-2, local-reader, writer, and read-back implementations live in
`adapters/odoo`. CLI, worker, storage-recovery, and target-adapter construction
live in `web/composition`.

Every old flat production path is deleted in the same change as its direct
imports. There are no runtime compatibility aliases. The temporary Phase 1
ownership manifest is deleted, `__main__.py` remains only as the package entry
point, and the architecture gate now fails on any flat, nested-unclassified,
or forbidden-direction production module.

**Exit condition:** Met. The deterministic inventory contains 363 production
modules, 1,996 runtime internal edges, one type-only edge, no unclassified
module, no runtime or type-inclusive cycle, and no application-to-adapter
edge.

### Phase 4: Organize tests and browser assets

Phase 4 is complete. Every discovered test now lives below an explicit
evidence level: `architecture`, `domain`, `application`, `integration`, `e2e`,
or `performance`. Capability subpackages then identify the owner or external
boundary. `tests/architecture/test_test_organization.py` fails on a new flat
test module, an oversized focused browser module, or a return of the historical
browser monolith.

The former 8,718-line `tests.test_web_app` module is deleted. Its 65 browser
contracts are grouped into Project setup, security, local-stack, source,
target, Mapping, Preparation, review, and load modules under
`tests/integration/web`; the complete setup journey lives under `tests/e2e`.
The three stale expectations recorded during Phase 0 were repaired against the
current global credential-dialog and source-snapshot contracts before the
split. Shared browser setup is non-discovered support, and
`ProjectWorkspaceBuilder` requires an explicit application context instead of
using process-global or order-dependent state.

The complete journey also exposed a real cross-process boundary defect: the
Data version projection canonicalized physical datasets by identity while the
workspace-only preparation worker could derive generated datasets from the
authored display order. `mapping_source_selection` now canonicalizes its
portable input by dataset identity, and a focused regression proves that
reversing physical display order cannot change the effective selection hash.
This keeps browser composition and isolated workers on one owner-independent
pure contract.

Browser assets now expose their ownership in their names and loading sites.
The shared `app.js` contains only cross-page behavior. Source review, schema,
normalization, review, execution, transformation impact, and job polling have
focused scripts. The Mapping page loads separate editor, value-rule, catalogue,
and viewport-position modules; the target page retains its own script and
stylesheet. The former `app.css` is deleted in favor of tokens, layout,
components, workflow-page styles, and existing Mapping and target-page styles.
No frontend build system was introduced.

The Mapping template entry point is now a 69-line composition of named
partials. Dataset identity, scalar and relationship catalogues, control totals,
validation, recovery actions, quality checks, and the value-match dialog each
have an explicit template. The field-catalogue endpoint renders the same
catalogue partials used by the full page, so progressive updates cannot drift
from the server-rendered form.

`tests/architecture/test_static_asset_ownership.py` protects asset order,
page-template ownership, the shared-script boundary, and reviewable module
sizes. Existing browser contracts and targeted source assertions prove client
contracts that matter to the server-rendered workflow; `node --check` protects
syntax. A separate client test runtime was not justified because Phase 4 moves
unchanged framework-free behavior and does not add a client-only state model.

**Exit condition:** Met. A focused change has a focused test command and does
not require editing a cross-feature browser asset unless the shared behavior
truly changes.

Verified on 2026-08-27: repository-root discovery ran 890 tests with 13
expected skips; focused web discovery ran 92 tests; the complete Project setup
journey passed; the 26-test integrated-run module passed in normal order and in
the isolated recorded orders `1729` and `20260826`; architecture inventory,
dependency, documentation, static-asset, JavaScript syntax, and diff-hygiene
gates passed.

### Phase 5: Promote the final organization to current architecture

Phase 5 is complete. `docs/architecture/code-organization.md` now owns the
implemented placement, ownership, dependency, transaction-port, browser-asset,
test, and review rules. The architecture overview and Python code map link that
guidance to the system model and exact navigation paths. The maintained
regression baseline now points to current architecture instead of treating
this plan as an active execution contract.

This plan is marked completed and remains only as historical delivery evidence.

**Exit condition:** Met. Current documentation describes the implemented
structure, and this delivery record is no longer current guidance.

Verified on 2026-08-27: documentation links, workflow ownership, code
orientation, deterministic inventory, dependency direction, test organization,
browser-asset ownership, and diff-hygiene gates passed. The architecture run
executed 56 tests with one expected Windows-only skip. Vale was not
installed, so its advisory prose review was not run.

## Delivery protocol used by maintainers and agents

The [current code-organization guide](../architecture/code-organization.md#placement-procedure)
owns the maintained placement and review procedure. The following checklist is
the protocol used during this delivery.

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

## Review triggers used during delivery

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

- [Current code organization](../architecture/code-organization.md)
- [Code architecture maintainability audit](../reports/code-architecture-maintainability-audit-2026-08-26.md)
- [Architecture overview](../architecture/overview.md)
- [Python code map](../architecture/python-code-map.md)
- [ADR-008 and ADR-014](../decisions/README.md)
- [Project and workspace lifecycle](../developer/contracts/project-lifecycle.md)
- [Integrated Test run lifecycle](../developer/contracts/integrated-run-lifecycle.md)
