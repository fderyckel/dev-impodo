# Migration projects and multi-Recipe cutover implementation plan

## Status and authority

**Status:** Accepted target architecture and active implementation plan from
2026-08-23. Phases M0 through M6 are complete; Phase M7 is next.

[ADR-014](../decisions/README.md#adr-014--migration-projects-coordinate-reusable-recipes-and-cutover-plans)
governs the target architecture. It supersedes ADR-012 and ADR-013 for
aggregate ownership, DataVersion ownership, and cutover coordination. It
retains the existing portable Recipe, fresh target-binding, qualification,
credential-separation, evidence, and fail-closed execution boundaries where
this plan does not explicitly replace them.

Current contracts, architecture pages, user documentation, and screenshots
describe the implemented Project-first browser through M5. This plan must not
be used to claim that a Production run is available before its later
implementation gate passes.

This plan replaces the completed
[Recipe-first test-to-production plan](reusable-recipes-and-data-versions-implementation-plan.md)
and its [Phase R0 contracts](reusable-recipes-phase-r0-contracts.md) as
forward-looking implementation authority. Those documents and their reports
remain historical evidence; only boundaries explicitly retained by ADR-014
remain current behavior.

## 1. Product outcome

Impodo's primary business object is a **Migration Project**. A data manager
creates a Project to prepare one governed migration effort from a legacy
system to Odoo 19. The Project can complete a one-off migration without a
Recipe, or it can contain several reusable Recipes.

The data manager first accepts representative source data and completes the
normal mapping, preparation, comparison, and review work. When that meaning is
safe to reuse, the data manager publishes it as an immutable Recipe revision.
Publishing does not convert the Project into a Recipe.

Before rollout, the data manager tests the selected Recipe revisions together
against one exact Test run. Impodo records their dependency order, rejects
overlapping writes, executes and reconciles them, and qualifies the combined
CutoverPlan. On rollout day, the data manager accepts a fresh complete data
package and applies that exact qualified plan to fresh Odoo target evidence.

The intended product promise is:

> Prepare and qualify reusable migration rules with representative data. On
> rollout day, apply the exact qualified Recipes to the latest complete data
> package, review current drift, and execute every migration unit in a safe
> dependency order with fresh evidence and approval.

### 1.1 First-release support boundary

The first reusable multi-Recipe release supports complete replacement CSV and
XLSX packages. It preserves the current Odoo-source capture and same-database
comparison path under the new Project ownership, but it does not publish that
path as a portable cross-database Recipe application or enable Odoo-source
writes.

A `PRODUCTION` MigrationRun records rollout intent and requires fresh
Production evidence. The label does not grant production write authority or
override ADR-010. Execution remains limited to targets and operations that the
current separately reviewed writer policy permits. General production
hardening or a target-side gateway remains a separate roadmap decision.

The first release also excludes cross-Project Recipe sharing, delta or inferred
delete semantics, mixed-target runs, arbitrary cross-Recipe merge rules,
unattended execution, and automatic rollback of already committed Odoo writes.

## 2. Historical baseline corrected by M0-M4

Before this plan started, the browser labelled a `Recipe` as a **project** and
the domain and registry made Recipe the aggregate root:

- `RecipeAuthoringService.create()` calls `ProjectService.create_project()`.
  That operation creates a `MigrationProject` workspace and generates a
  `recipe_id` and `data_version_id` in the same operation.
- `Recipe` owns `current_recipe_revision`, `current_data_version_id`, and
  `cutover_candidate_id`.
- `DataVersion` contains `recipe_id` and one `workspace_project_id`.
- `RecipeApplicationService.start_test_data_version()` and
  `start_production_data_version()` create successor workspaces from one
  Recipe.
- `RecipeQualificationService` qualifies one Recipe revision and selects one
  Recipe-owned cutover candidate.
- `/recipes` and `/recipes/new` are the list and creation routes, while
  `/projects/{workspace_project_id}` identifies the contained workspace.
- `registry.py` persists Recipe-owned DataVersions and retains migrations for
  earlier Recipe-root schema shapes.

This structure safely supports one reusable migration unit, but it cannot
represent a Project with no Recipe, several Recipes sharing one accepted data
package, or one integrated cutover plan. The browser rename hid that limitation
instead of resolving it.

M0 through M4 corrected this ownership and integrated-planning boundary
without replacing the working mapping, preparation, comparison, execution, or
reconciliation engines. The bullets above are historical context, not current
routes, schema, or ownership.

## 3. Target vocabulary and cardinality

### 3.1 MigrationProject

`MigrationProject` is the operator-facing business identity and project-level
governance root. It owns the migration purpose, source-system identity,
classification, retention policy, lifecycle, DataVersion lineage,
MigrationRun lineage, Recipe membership, and CutoverPlan lineage.

A Project can exist before source data is accepted and before any Recipe is
published. Completing a one-off Project does not require publishing a Recipe.

At product level, a Project contains Recipes. In code, `MigrationProject` and
`Recipe` remain separate aggregate roots connected by immutable `project_id`.
Repositories must not load every Recipe or workspace to read or update one
Project.

### 3.2 DataVersion

`DataVersion` is one Project-owned source package. While it is a draft, the
data manager may assemble and inspect its files. Acceptance freezes its exact
CSV, XLSX, or supported Odoo-source membership, purpose, and as-of context;
the accepted DataVersion is immutable.

Examples include:

- `Representative export - 15 August`;
- `Integrated Test export - 25 August`; and
- `Rollout export - 31 August, 18:00 cutoff`.

DataVersion owns source-file identities, accepted table selections, source
snapshots, source hashes, completeness controls, and source-package
provenance. An abandoned draft never becomes accepted evidence. DataVersion
does not belong to one Recipe. Several Recipe applications may consume
different logical datasets from the same accepted DataVersion.

### 3.3 MigrationRun

`MigrationRun` coordinates one Authoring, Test, or Production use of one exact
DataVersion against one exact Odoo target. The first release permits one Odoo
target identity per run.

The run owns the current `TargetBinding`, credential generations, unioned Odoo
requirements plan, target schema and reference snapshots, application order,
and integrated readiness. A Recipe application references the run's exact
target binding and receives only the bounded target projection it needs.

This ownership prevents one connection, schema, permission, or reference read
per Recipe. The run planner unions compatible requests by Odoo model and field
and keeps all Odoo 19 reads bounded and batched.

### 3.4 MigrationWorkspace

`MigrationWorkspace` is the internal technical boundary for one mapping and
execution unit inside a MigrationRun. It replaces the current internal use of
the name `MigrationProject`.

One Authoring workspace may remain a one-off migration unit or publish one
Recipe identity. One Test or Production workspace applies one exact Recipe
revision. A workspace stores its mapping, preparation, quality, comparison,
approval, execution, read-back, reconciliation, audit, and recovery evidence.

The workspace references its Project, DataVersion, and MigrationRun through
distinct identifiers. A Recipe application additionally references one exact
Recipe revision. Identifier resolution rejects values supplied in the wrong
namespace.

### 3.5 Recipe and RecipeRevision

`Recipe` is a Project-scoped, separately authorized aggregate root for one
coherent reusable migration purpose. A Recipe has an immutable `project_id`, a
name, a business purpose, classification and retention projections, optimistic
revision, and immutable RecipeRevision lineage.

Creating a Project does not create an empty Recipe shell. The first Recipe and
RecipeRevision are created together only when the data manager publishes
eligible meaning from an Authoring workspace. Later semantic changes create
new immutable revisions of that Recipe.

One Recipe may cover several logically related source datasets and several
Odoo models when they share one purpose, owner, qualification lifecycle, and
dependency order. Impodo must not create one Recipe per Odoo model.

### 3.6 RecipeApplication

`RecipeApplication` records how one exact RecipeRevision applies to one exact
DataVersion, MigrationRun, TargetBinding, and MigrationWorkspace. It owns
physical dataset and column bindings, per-run parameters and controls, focused
drift issues, mapping materialization evidence, and terminal readiness.

A RecipeApplication never changes the published Recipe. A change to reusable
meaning creates a new RecipeRevision. A physical binding override or declared
parameter value applies only to the current application.

### 3.7 CutoverPlan and qualification

`CutoverPlan` is a Project-scoped, versioned plan that selects one exact
RecipeRevision for each participating Recipe. A CutoverPlan revision also
stores a directed acyclic dependency graph, declared write ownership, shared
controls, and the required Recipe qualifications.

One integrated Test run qualifies one exact CutoverPlan revision after all of
its Recipe applications complete preparation, comparison, execution,
read-back, and reconciliation in the declared order. Project-level cutover
selection pins that qualification. Production never substitutes a newer
Recipe or CutoverPlan revision.

A Project with one Recipe still uses a one-item CutoverPlan. This avoids a
separate single-Recipe production path.

### 3.8 Required relationships

```text
MigrationProject 1
|-- DataVersion 0..many
|-- MigrationRun 0..many
|   |-- MigrationWorkspace 1..many
|   `-- RecipeApplication 0..many
|-- Recipe 0..many
|   `-- RecipeRevision 1..many
`-- CutoverPlan 0..many
    `-- CutoverPlanRevision 1..many
        |-- selected RecipeRevision 1..many
        `-- dependency edges 0..many
```

## 4. Recipe semantic contract

### 4.1 Reusable meaning

A RecipeRevision may contain:

- logical source datasets and required logical columns;
- target Odoo models, fields, and intended write roles;
- business keys and scope;
- scalar providers, transformations, value mappings, and null policies;
- relationship resolvers and missing or ambiguous policies;
- derived-entity and preparation rules that satisfy portability requirements;
- Odoo 19 target requirements without one target identity;
- reusable quality and quarantine rules;
- declared parameters and their allowed use sites;
- reusable control definitions;
- categorical coverage policies;
- dependency declarations within the Recipe; and
- stable content hashes and compatibility hints.

### 4.2 Excluded evidence

A RecipeRevision must exclude:

- source rows, source-file IDs, dataset IDs, column stable keys, and source
  hashes;
- one endpoint, database, credential, principal, permission set, company
  context, or target fingerprint;
- physical Odoo schema or reference snapshots;
- numeric Odoo record IDs;
- per-DataVersion parameter values or expected control totals;
- approvals, comparison results, execution snapshots, write journals,
  read-back, or reconciliation results; and
- one Project workspace or MigrationRun identity as reusable meaning.

### 4.3 Recipe boundary test

Rules belong to one Recipe when the data manager must version, test, qualify,
and rerun them together to achieve one coherent business outcome.

Rules should use separate Recipes when they have independent owners, source
delivery cadence, qualification criteria, target write ownership, recovery,
or cutover decisions. A Product and BOM migration may form one Recipe when the
dependency is inseparable. Customer master data and opening stock normally
form separate Recipes even when they belong to the same Project.

## 5. Source-package and latest-data boundary

### 5.1 DataVersion acceptance

The data manager creates a new DataVersion instead of replacing accepted
source files. Acceptance records the complete package membership, hashes,
logical dataset inventory, as-of context, actor, and time.

When several files form one business snapshot, DataVersion acceptance requires
an explicit completeness decision. Impodo does not infer that independently
exported files are mutually consistent merely because they were uploaded near
the same time.

### 5.2 Shared source storage

The local composition must separate project-level source-package storage from
application workspaces:

- one DataVersion store owns source files, inspection catalogues, accepted
  table selections, and immutable source snapshots;
- each MigrationWorkspace references the exact DataVersion and materializes
  only its selected logical datasets through an application-owned port; and
- content-addressed artifacts may be shared read-only, but workspace current
  pointers, mappings, credentials, preparation, approvals, and execution
  evidence are never shared.

The implementation must not copy the current workspace DuckDB to create a new
DataVersion or Recipe application.

### 5.3 Drift assessment

Applying a RecipeRevision to a later DataVersion must distinguish:

- compatible physical drift, such as column reordering;
- application-only binding overrides, such as an explicitly confirmed renamed
  physical column;
- declared parameter values that legitimately vary per run;
- new unused source structure, which is informational;
- uncovered categorical or relationship values, which require review; and
- semantic changes, which create a new RecipeRevision and require renewed Test
  qualification.

Missing source rows never imply target deletion. Delta semantics remain a
separate explicit future policy.

## 6. Multi-Recipe planning and execution

### 6.1 Dependency graph

Every CutoverPlan revision stores explicit dependencies between selected
Recipe revisions. The planner rejects self-dependencies, missing nodes, and
cycles before it creates execution readiness.

Dependencies use portable business meaning. They do not use target numeric
IDs. An upstream application must reconcile its committed results before a
dependent application begins.

### 6.2 Write ownership and collision detection

Each RecipeRevision declares its intended Odoo write set by model, field role,
business identity, and scope. Before Test or Production execution, the run
planner compares every selected write set.

The planner blocks:

- two Recipes that may write the same field of the same business identity
  without an explicit supported merge contract;
- an implicit last-writer-wins order;
- incompatible create or update modes for the same record domain;
- a required-at-create relationship cycle; and
- a Recipe that reads target state invalidated by an earlier Recipe without a
  planned refresh boundary.

The first release should prefer one declared owner for each writable record
field. General merge semantics are out of scope.

### 6.3 Shared target reads

`MigrationRunPlanner` compiles the union of selected Recipe target
requirements. Odoo metadata, identity, reference, and comparison reads remain
closed, deterministic, and batched by model.

No Odoo adapter may call `fields_get`, `search_read`, `browse`, or another ORM
or RPC operation inside a source-row loop or once per Recipe when the same
run-level result can be captured once. Query-budget tests must cover the number
of Recipes, Odoo models, datasets, and source rows independently.

### 6.4 Execution and recovery

The executor starts applications only in the validated dependency order. Each
application keeps its own execution snapshot and write journal. The run keeps
an integrated progress and outcome projection.

An unknown write outcome must reconcile before retry or before a dependent
Recipe starts. One failed application does not erase honest evidence from
completed applications, but it blocks overall run qualification.

A MigrationRun coordinates several applications but does not claim one global
Odoo database transaction across separate requests. If an upstream application
commits and a later application fails, Impodo preserves the partial outcome,
stops dependent work, and directs the operator through exact reconciliation
and an explicitly supported recovery. It does not issue a guessed compensating
write or describe the run as rolled back.

## 7. Target, credential, and authorization boundaries

One MigrationRun binds one exact Odoo target and purpose. Test and Production
runs always create independent bindings, even when an endpoint happens to be
the same.

The run stores only non-secret target, principal, permission, context, schema,
reference, probe, and credential-generation evidence. API keys remain in the
governed secret store. Read and write roles never fall back to one another.

Credential rotation changes the run binding generation and invalidates every
dependent comparison and execution readiness projection. It does not change
the DataVersion, RecipeRevision, or historical qualification.

Authorization is Project scoped and action specific. Recipe, DataVersion,
run, application, workspace, plan, and qualification identifiers cannot grant
access without current Project membership and the required capability.

The first release does not apply one Recipe across Projects. Future sharing
must use an explicit export, review, and import operation that creates a new
Project-scoped Recipe identity with lineage. It must not introduce mutable
cross-Project ownership or authorization leakage.

## 8. Persistence and clean development cutover

### 8.1 New schema generations

Because Impodo is still in development, this change uses clean schema
generations instead of another compatibility layer. The local composition
must define new exact generations for:

- the cross-Project registry;
- the DataVersion source-package database; and
- the MigrationWorkspace database.

An older Recipe-first registry or workspace must fail closed with an explicit
development-reset message. Impodo must not backfill a Project shell, hydrate
legacy setup fields, dual-write old and new tables, or reinterpret an old
workspace as the new model.

The repository must provide an explicit developer-only reset procedure that
enumerates the exact Impodo storage root and requires confirmation. Runtime
startup must never delete an old database automatically.

### 8.2 Target registry shape

The new registry requires bounded tables or equivalent repository contracts
for:

- `migration_project`;
- `data_version`;
- `migration_run`;
- `migration_workspace`;
- `recipe` and `recipe_revision`;
- `recipe_application`;
- `recipe_qualification`;
- `cutover_plan`, `cutover_plan_revision`, selected Recipe revisions, and
  dependency edges;
- integrated plan qualification and selected cutover candidate;
- non-secret TargetBinding projections;
- restart-safe operation intents; and
- bounded list and status projections.

The exact schema must enforce distinct identifiers, Project ownership, unique
version numbers within their parent, immutable revision records, and exact
foreign-key relationships through repository validation.

### 8.3 Workspace linkage

The current `recipe_workspace_linkage` table must be removed. The new
workspace database stores one exact linkage record containing
`workspace_id`, `project_id`, `data_version_id`, `migration_run_id`, and an
optional `recipe_application_id`.

The linkage is written at workspace creation and never adopted later. Opening
a workspace verifies the registry relation before reading mutable state.

### 8.4 Protected stores and deletion

Published Recipe and qualification payloads remain application encrypted.
Protected storage keys include exact Project and Recipe ownership bindings.

Project deletion must begin at the Project boundary and persist an exact
enumeration of DataVersion stores, workspaces, Recipe keys, qualification
keys, credentials, artifacts, and registry rows before cleanup. Published
Recipes may be archived individually, but referenced immutable revisions are
not silently deleted.

No repository or route may retain Recipe-root deletion, standalone workspace
deletion, bootstrap adoption, legacy backfill, or compatibility-shell code
after the clean-root gate passes.

## 9. Application and domain refactor map

### 9.1 Domain modules

The implementation should create or extract these explicit domains:

- `migration_projects.py` owns `MigrationProject`, Project lifecycle, and the
  Project repository port.
- `data_versions.py` owns Project source-package identity, lifecycle, and
  repository ports.
- `migration_runs.py` owns run purpose, TargetBinding, union requirements,
  readiness, and integrated state.
- `migration_workspaces.py` owns the internal workspace identity and exact
  linkage contract.
- `recipes.py` retains Recipe, RecipeRevision, publication, and protected
  payload contracts but removes DataVersion and cutover ownership.
- `domain/recipe_applications.py` binds RecipeRevision, DataVersion, run, and
  workspace explicitly.
- `domain/cutover_plans.py` owns plan revisions, selected Recipes,
  dependencies, write ownership, qualification, and cutover selection.

The current `projects.MigrationProject` workspace class must be renamed rather
than retained as a second meaning of Project. Temporary type aliases are not
permitted in the completed change.

### 9.2 Application services

The target service responsibilities are:

- `MigrationProjectService` creates, governs, closes, archives, and deletes
  Projects.
- `DataVersionService` accepts and freezes complete source packages.
- `MigrationRunService` creates Authoring, Test, and Production runs and binds
  their target evidence.
- `MigrationWorkspaceService` provisions isolated mapping and execution
  workspaces.
- `RecipePublicationService` publishes an eligible workspace as a first Recipe
  revision or a successor revision.
- `RecipeApplicationService` assesses and materializes one exact application;
  it no longer creates DataVersions or owns Production selection.
- `CutoverPlanService` validates selected revisions, dependencies, write
  ownership, and integrated controls.
- `MigrationRunExecutionService` sequences applications and exposes integrated
  progress without bypassing the existing per-workspace execution and
  reconciliation services.
- `QualificationService` publishes Recipe and integrated CutoverPlan
  qualification evidence.

`RecipeAuthoringService.create()` must be deleted. Project creation must not
flow through Recipe services.

### 9.3 Existing engines to retain

The following behavior should be adapted through ports rather than rewritten:

- source inspection and immutable source snapshots;
- Odoo 19 schema and reference capture;
- mapping contracts and validation;
- reusable Recipe compilation and protected publication validation;
- canonical staging and preparation;
- quality, quarantine, and normalization review;
- deterministic comparison and execution-snapshot creation;
- closed Odoo readers and restricted writer;
- write journals, read-back, and reconciliation; and
- hash-bound audit and invalidation rules.

## 10. Browser workflow

### 10.1 Project creation and overview

`/projects` becomes the only list route and `/projects/new` becomes the only
creation route. Choosing **New project** creates a MigrationProject, its first
Authoring DataVersion, its first Authoring MigrationRun, and one empty
MigrationWorkspace. It does not create a Recipe.

The Project overview shows:

- the current data package and its completeness state;
- the Authoring, Test, and Production run history;
- zero or more Recipes and their current revisions;
- current CutoverPlan readiness; and
- one obvious next action.

### 10.2 Authoring and publication

The existing six-stage workspace remains the normal mapping and evidence
experience. The operator can complete and execute a one-off workspace without
publishing reusable meaning.

When the workspace has an eligible submitted mapping, the Project overview
offers **Save as Recipe**. The action asks for a Recipe name and purpose and
creates the Recipe plus revision 1 in one idempotent operation. Publishing a
successor revision remains an explicit action from a workspace associated with
that Recipe.

To author another independent Recipe, the operator creates another migration
workspace in the Project and selects the logical datasets it owns. The first
release does not split one submitted mapping automatically into several
Recipes.

### 10.3 Test and rollout

The operator selects Recipe revisions for a Test plan. Impodo presents
dependency and write-collision problems before creating Test application
workspaces.

After an integrated Test run qualifies, the operator explicitly selects its
CutoverPlan revision. **Run with latest data** creates a new Project-owned
DataVersion and Production MigrationRun. The selected Recipe revisions remain
pinned while the operator reviews only current source, target, reference,
parameter, control, or credential drift.

### 10.4 Route removal

The completed change removes:

- `/recipes/new` as a Project creation alias;
- `/recipes` as the global Project list;
- Recipe-owned Test and Production DataVersion creation routes;
- internal use of `/projects/{workspace_project_id}` as if the workspace ID
  were a business Project ID; and
- redirects that exist only to preserve the Recipe-first ownership model.

Project, Recipe, DataVersion, run, plan, application, and workspace routes
must use their own identifiers and resolve Project authorization explicitly.

## 11. Concurrency, idempotency, and recovery

Every state-changing operation uses an expected aggregate revision and a
stable request identity where browser retry could duplicate work.

Restart-safe intents are required for:

- Project plus initial DataVersion, run, and workspace creation;
- DataVersion acceptance and immutable source publication;
- Recipe plus first revision publication;
- successor RecipeRevision publication;
- multi-Recipe run and workspace provisioning;
- Recipe and CutoverPlan qualification;
- cutover selection;
- credential rotation invalidation; and
- Project deletion target enumeration and cleanup.

An intent names its owning aggregate instead of using one generic Recipe ID.
Recovery validates every referenced identifier and content hash. It never
parses an error message to decide ownership or navigation.

## 12. Performance and scale invariants

The Project list, Project overview, Recipe list, DataVersion history, run
history, and CutoverPlan overview use registry projections and open no source
or workspace DuckDB databases.

The implementation must add query-budget acceptance for at least:

- 100 Projects with no workspace opens during list rendering;
- one Project with 25 Recipes and 10 DataVersions;
- one Test run with 10 Recipe applications;
- Odoo metadata and record calls bounded by unioned model and request batches,
  not Recipe count or source-row count;
- one source dataset scanned once for each declared compatibility or
  categorical purpose, not once per mapped field; and
- integrated status rendering from registry or in-memory job projections,
  not repeated workspace schema checks.

No new Project or Recipe path may introduce Odoo N+1 reads, per-row repository
queries, Python UDF fallback for otherwise supported columnar rules, or
unbounded distinct-value materialization.

## 13. Delivery phases

All phases should be developed on one cutover branch. The Recipe-first runtime
must not be released as a supported compatibility mode beside the new model.
The branch is not complete until Phase M7 deletes the old ownership path.

### Phase M0 - Freeze target contracts and acceptance fixtures

**Status:** Completed on 2026-08-22. This phase added architecture contracts
and executable fixtures only; it changed no runtime behavior. See the
[Phase M0 contracts](migration-projects-phase-m0-contracts.md).

- Add executable identity, cardinality, Recipe boundary, DataVersion,
  MigrationRun, application, CutoverPlan, collision, dependency, and
  qualification contracts.
- Add a deterministic fictional Project fixture containing Customer and
  Product/BOM Recipes plus a later rollout DataVersion.
- Freeze the retained Recipe semantic envelope and identify every field whose
  ownership changes without changing its reusable meaning.
- Record exact out-of-scope behavior: cross-Project Recipe sharing, arbitrary
  merge semantics, delta deletes, mixed-target runs, and unattended rollout.

**Gate:** fixtures prove that Project, DataVersion, Recipe, run, plan,
application, and workspace identifiers are distinct and that a Project can
have zero, one, or several Recipes. The focused M0 suite passes 11 tests,
including fail-closed mutations for Project membership, mixed targets,
dependency cycles, write collisions, and qualification drift.

### Phase M1 - Introduce clean Project, DataVersion, run, and workspace roots

**Status:** Completed on 2026-08-22. See the [Phase M1 persistence
foundation](migration-projects-phase-m1-foundation.md). The clean services and
stores exist but are not composed into the current browser.

- Implement the new domain modules and repository ports.
- Create the new exact registry, DataVersion-store, and workspace schema
  generations.
- Create Project-native operation intents and bounded registry projections.
- Reject old Recipe-first storage with an explicit development-reset path.
- Add authorization, optimistic concurrency, idempotency, and fault-injection
  tests for each new root.

**Gate:** the new repositories create and resolve exact relationships without
dual-writing or reading any Recipe-first table. The focused M1 suite passes 11
tests covering exact schemas, four distinct roots, bounded Project lists,
authorization, optimistic concurrency, idempotency, injected faults,
identifier confusion, old-storage rejection, and recoverable reset.

### Phase M2 - Separate source packages from application workspaces

**Status:** Completed on 2026-08-22. See the [Phase M2 source-package
foundation](migration-projects-phase-m2-source-packages.md). Phase M3 now
composes these services and stores into the current browser.

- Move source intake, catalogues, table selection, and source snapshots to the
  DataVersion boundary.
- Adapt current source and mapping services to materialize one bounded
  read-only DataVersion projection into a MigrationWorkspace.
- Rename the current internal `MigrationProject` workspace across domain,
  adapters, services, presenters, tests, and documentation.
- Preserve hash-bound invalidation and preparation semantics.

**Gate:** two workspaces can consume different logical datasets from the same
DataVersion without copying mutable state or sharing current pointers. The M2
suite passes ten tests covering exact schemas, incremental file intake, file
and Odoo package origins,
canonical package identity,
freeze immutability, bounded projections, authorization, optimistic
concurrency, fault recovery, and non-mutating M1-storage rejection.

### Phase M3 - Switch Project creation and optional Recipe publication

**Status:** Completed on 2026-08-22. See the [Phase M3 implementation
record](migration-projects-phase-m3-project-authoring.md).

- Replace `/recipes` and `/recipes/new` with Project-native routes.
- Create the first Authoring DataVersion, run, and workspace without a Recipe.
- Support one-off completion without Recipe publication.
- Publish Recipe plus revision 1 atomically from one eligible workspace.
- Publish successor revisions without moving DataVersion or cutover ownership
  back to Recipe.
- Remove Recipe-root creation, deletion, bootstrap, and shell paths.

**Gate:** a new Project is visible and usable with no Recipe; publishing adds
one Recipe without changing the Project or DataVersion identity.

### Phase M4 - Apply several Recipes in one MigrationRun

**Status:** Complete on 2026-08-22. See the
[M4 implementation record](migration-projects-phase-m4-multi-recipe-runs.md).

- Implement run-level TargetBinding and unioned Odoo requirements planning.
- Create one application workspace per selected RecipeRevision.
- Add dependency-graph and write-collision validation.
- Reuse the existing application compiler to create fresh mappings and focused
  drift issues.
- Add integrated progress projections and bounded query tests.

**Gate:** Customer and Product/BOM Recipes apply to one Test DataVersion and
target with no duplicate target capture, no overlapping writes, and no shared
mutable workspace state.

### Phase M5 - Qualify an integrated CutoverPlan

**Status:** Completed on 2026-08-22. See the [Phase M5 implementation
record](migration-projects-phase-m5-cutover-qualification.md).

- Publish individual Recipe qualification from exact application evidence.
- Publish integrated CutoverPlan qualification only after ordered execution,
  read-back, reconciliation, and shared controls pass.
- Make plan revisions immutable and make later Recipe revisions unqualified
  for that plan.
- Select one exact Project cutover candidate separately from qualification.

**Gate:** changing one selected Recipe revision or dependency edge creates a
new unqualified plan revision; no earlier qualification transfers.

### Phase M6 - Run the qualified plan with latest data

**Status:** Completed on 2026-08-23. See the [Phase M6 implementation
record](migration-projects-phase-m6-production-rollout.md).

- Accept a fresh complete Production DataVersion.
- Create a fresh Production MigrationRun with independent target, read
  credential, write credential, schema, references, parameters, controls,
  comparison, approval, execution, and reconciliation evidence.
- Pin the selected CutoverPlan and all Recipe revisions exactly.
- Block uncovered values, structural drift, target incompatibility, credential
  rotation, write collisions, and stale controls with owning recovery actions.

**Gate:** the integrated qualified plan runs against the latest package and a
different compatible Odoo 19 target without using Test credentials or evidence
as Production readiness.

### Phase M7 - Remove Recipe-first ownership and complete documentation

**Status:** Planned and blocking.

- Delete Recipe-owned DataVersion and cutover fields, services, repository
  methods, schema migrations, routes, templates, presenters, tests, and type
  names.
- Delete `recipe_workspace_linkage`, Project-as-workspace naming, shell
  backfill, bootstrap adoption, and compatibility resolution.
- Remove superseded fixtures that encode Recipe as Project.
- Update all current contracts, architecture pages, workflow pages, code
  docstrings, code maps, BPMN models, screenshots, examples, and tests in the
  same change.
- Retain old reports only as labelled point-in-time evidence. Use Git history
  instead of keeping stale active architecture summaries.

**Gate:** scoped searches find no active claim that Project equals Recipe, no
Recipe-owned DataVersion or cutover pointer, no `/recipes/new` creation path,
no old schema migration, and no compatibility alias.

## 14. Acceptance scenarios

### 14.1 Recipe-less Project

A data manager creates a Project, accepts representative files, completes the
six-stage workflow, and performs a permitted one-off rehearsal without
publishing a Recipe. Project and DataVersion history remain complete.

### 14.2 Publish the first Recipe after mapping

A submitted Customer mapping publishes Customer Recipe revision 1. The
Project and source DataVersion keep their identifiers. Publishing the same
meaning again is idempotent and does not create a duplicate revision.

### 14.3 Several Recipes share one data package

Customer and Product/BOM workspaces bind different logical datasets from one
accepted Test DataVersion. They have isolated mappings and evidence but share
the immutable source package and run-level target binding.

### 14.4 Related models stay in one Recipe

Product, variants, BOM headers, and BOM lines remain one Recipe when their
source preparation and dependency order form one qualification unit. The
planner does not split them by Odoo model.

### 14.5 Cross-Recipe dependency

A dependent Recipe references records created by an upstream Recipe through
portable business keys. The upstream application reconciles before the
dependent application starts.

### 14.6 Overlapping writes

Two Recipes may write `res.partner.name` for the same scoped business key. The
plan blocks before comparison or execution. Reordering the Recipes does not
silently resolve the conflict.

### 14.7 Cyclic plan

Recipe A depends on Recipe B and Recipe B depends on Recipe A. Plan publication
fails with both affected Recipes and one direct recovery action.

### 14.8 New rollout values

The rollout export introduces `German` and `LUX`. Compatible rules remain
reused, but uncovered selection and relationship values block. No rule guesses
a code or numeric Odoo ID.

### 14.9 Semantic rollout correction

The operator changes a reusable transformation after Production drift review.
Impodo creates a new Recipe revision and makes the selected CutoverPlan
unqualified. It does not hide the change as a Production-only override.

### 14.10 Complete latest-data package

The operator uploads several files with different export times. Impodo does
not call them one complete DataVersion until the operator confirms the package
membership and as-of context and required controls pass.

### 14.11 Credential rotation

The Production read key changes after comparison. The run receives a new
credential generation, invalidates target-dependent readiness, and requires a
fresh probe and comparison without changing Recipe or source evidence.

### 14.12 Unknown write outcome

An upstream application loses its response after an Odoo write. Impodo
reconciles the journal before retry and before starting dependent Recipes.

### 14.13 Partial multi-Recipe completion

Customer migration commits and reconciles, then Product/BOM migration fails.
Impodo retains the successful Customer evidence, marks the integrated run
incomplete, stops dependent applications, and presents the exact supported
recovery. It does not claim an all-or-nothing rollback.

### 14.14 No inferred delete

A record present in Test but absent from the rollout package is not deleted,
archived, or proposed for write merely because it is missing.

### 14.15 Authorization and identifier isolation

Supplying a Recipe, DataVersion, run, application, plan, or workspace ID where
a Project ID is required fails. An actor cannot use membership in another
Project to read Recipe payloads, target evidence, credentials, or workspaces.

### 14.16 Bounded performance

Listing Projects and Recipes opens no workspaces. Adding Recipes does not add
one identical Odoo schema capture per Recipe, and adding source rows does not
add Odoo or repository calls per row.

## 15. Documentation cutover matrix

### 15.1 Update immediately with this plan

- `docs/decisions/README.md` records ADR-014 and supersedes ADR-012 and
  ADR-013.
- `docs/README.md` and `docs/plans/remaining-work.md` identify this plan as the
  active forward-looking priority.
- The completed Recipe-first plan and Phase R0 contracts receive historical
  supersession notices without rewriting their implementation record.
- `docs/product-vision.md` distinguished the then-current Recipe-first code
  from the accepted Project target.

### 15.2 Update with implemented behavior

Each delivery gate must review and update:

- `docs/workflow.yml`;
- `docs/architecture/overview.md` and `python-code-map.md`;
- `docs/developer/contracts/recipe-lifecycle.md`, `project-lifecycle.md`, and
  `evidence-lifecycle.md`;
- the setup and all affected developer workflow pages;
- `docs/user/getting-started.md` and affected user workflow pages;
- `docs/glossary.md` and `docs/product-vision.md`;
- BPMN models and captions;
- code docstrings and the code-documentation inventory;
- current screenshots captured at 1440x1024 with fictional data;
- acceptance, security, release, and recovery documentation; and
- all inbound links to superseded Recipe-first authorities.

Reports remain point-in-time evidence and receive a historical banner only
when a reader could otherwise mistake them for current authority.

## 16. Verification strategy

Each phase runs its focused domain, repository, service, browser, security,
and fault-injection suites. The clean-root phase also runs:

```powershell
.\.venv\Scripts\python.exe scripts\documentation_quality.py --check
.\.venv\Scripts\python.exe scripts\code_documentation_inventory.py --check
.\.venv\Scripts\python.exe -m unittest tests.test_documentation_quality -v
.\.venv\Scripts\python.exe -m unittest tests.test_code_documentation -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
git status --short
```

Browser acceptance must cover Project creation, Recipe publication,
multi-Recipe Test planning, collision recovery, qualification, latest-data
Production setup, and current screenshots. Odoo acceptance must use Odoo 19
and record bounded request counts. Any unavailable browser, remote Odoo,
Windows, security, or full-suite evidence must be reported explicitly.

## 17. Approaches explicitly rejected

### Keep Project as a browser alias for Recipe

Rejected because it prevents Recipe-less Projects and multi-Recipe cutover
while making one identifier carry two meanings.

### Restore `ProjectSeries` above the current Recipe root

Rejected because it would preserve the wrong ownership and add another shell.
The new `MigrationProject` is the actual business root, not a compatibility
wrapper.

### Create an empty Recipe with every Project

Rejected because a Recipe should exist only when the operator publishes
reusable meaning. Empty Recipe shells obscure whether reuse is intentional.

### Make one Recipe per Odoo model

Rejected because related models often form one business outcome and require
one preparation, dependency, and qualification lifecycle.

### Store several Recipes in one mutable workspace

Rejected because singleton current pointers, invalidation, credentials,
approvals, journals, and recovery would leak across applications. Each
application receives an isolated workspace.

### Capture the same Odoo target independently for every Recipe

Rejected because it duplicates remote reads, can observe inconsistent target
states, and creates Recipe-count query growth. One run owns one exact binding
and unioned requirements plan.

### Keep old schemas through automatic backfill or dual writes

Rejected because the product is in development and the compatibility code
would become permanent risk. Old storage fails closed and developers reset it
explicitly.

### Treat a Recipe qualification as integrated cutover qualification

Rejected because independently safe Recipes can still conflict, form a cycle,
or fail shared controls when combined.

### Infer deletion from a replacement export

Rejected because exports can be filtered, incomplete, or inconsistent.
Deletion requires a separate explicit policy and evidence contract.

## 18. Definition of done

This architecture is complete only when:

- MigrationProject is the operator-facing and domain business root;
- a Project can exist and complete a one-off path without a Recipe;
- one Project can contain several separately versioned Recipes;
- DataVersion belongs to Project and owns one immutable complete source
  package;
- MigrationRun owns one exact target binding and unioned Odoo requirements;
- every RecipeApplication binds an exact RecipeRevision, DataVersion, run, and
  isolated workspace;
- a Recipe can cover one or more logically related datasets and Odoo models;
- multi-Recipe dependencies are acyclic and overlapping writes fail closed;
- individual Recipe qualification does not replace integrated CutoverPlan
  qualification;
- the selected Project cutover candidate pins the exact plan, Recipe
  revisions, dependencies, and qualification;
- Production uses a fresh DataVersion, target binding, credentials, schema,
  references, parameters, controls, comparison, approval, execution, and
  reconciliation evidence;
- Recipe semantics exclude source rows, target identity, credentials, numeric
  Odoo IDs, approvals, and execution evidence;
- no path introduces Odoo or repository N+1 work;
- old Recipe-root schemas fail closed and require an explicit development
  reset;
- all Recipe-first creation, ownership, shell, backfill, alias, schema,
  service, route, test, and documentation code is removed;
- current documentation and screenshots describe the implemented Project
  workflow without stale Recipe-first claims; and
- focused, full-regression, security, browser, Odoo 19, documentation, and
  Windows acceptance evidence passes or every environmental omission is
  disclosed.
