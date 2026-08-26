---
audience: developer
stage: integrated-test
status: current
---

# Integrated multi-Recipe Test run

## Responsibility

This workflow plans and provisions one Project-owned Test `MigrationRun` over one frozen
Test `DataVersion`. It pins exact Recipe revisions, validates dependencies and
conservative field-level write ownership, stores one run-level target
projection, and creates one isolated `RecipeApplication` and
`MigrationWorkspace` per selected Recipe.

Planning creates fresh mapping drafts and focused readiness issues, then binds the
run to a CutoverPlan revision immediately after provisioning. Planning does
not execute applications, publish qualification, or grant Production
authority.

## Entry conditions

The Project must own at least one exact protected Recipe revision from an
accepted Authoring DataVersion. The browser then creates a fresh draft Test
DataVersion, Test MigrationRun, and shared setup MigrationWorkspace before it
accepts source or target evidence.

## Implementation flow

### Browser entry

`GET /projects/{project_id}/test-runs/new` reads the Project and its bounded
Recipe projection. The data manager selects exact Recipe revisions, any
explicit dependency edges, and the newer delivery cutoff.

`POST /projects/{project_id}/test-runs/new` invokes
`TestRunSetupService.start_setup`. One restart-safe operation creates the draft
Test DataVersion, draft Test MigrationRun, shared setup MigrationWorkspace,
source package, and `TestRunSetupBinding`. The binding pins exact Recipe
semantic hashes and dependency order before the browser redirects to
`GET /projects/{project_id}/test-runs/{migration_run_id}/fresh-data`.

The run-owned **Fresh data** page calls
`TestRunSetupService.fresh_data_requirements`. It shows the logical source
tables and columns declared by each exact Recipe revision, ordered by the
run's dependencies. `RecipeService.read_revisions` reads the selected Recipe
and revision rows through one registry connection, including archived Recipe
identities, then verifies each protected envelope. The number of registry
connections does not grow with the number of Recipes. Protected envelope
reads remain one per exact selected revision.

The page shows files already attached to the Test DataVersion. Its upload and
removal forms post to run-owned routes that first verify Project, run, and
setup-workspace ownership. `accept_source_uploads` and `remove_source_file`
adapt both the run routes and ordinary Authoring routes to the same governed
intake service, revision checks, protected storage, file validation, cleanup,
and audit path. The run-owned registration action inspects the current files
and returns to **Fresh data**. `fresh_data_match_plan` reads the resulting
catalogue set once, deduplicates shared logical inputs across Recipes, and
matches tables in memory from unique normalized required headers. It excludes
formula and error tables and prevents one physical table from filling two
different logical inputs. It also prevents a worksheet and an Excel named table
covering the same workbook area from filling separate inputs. A unique safe
match is automatic; the form posts an explicit choice only for a remaining
ambiguity.

Acceptance confirms the selected tables through `SourceWorkspaceService`,
assigns deterministic dataset names derived from the Recipe logical dataset
IDs, freezes the existing immutable source selection, and projects it into the
Test DataVersion. An interrupted projection can resume from that frozen
selection. The ordinary Authoring source pages keep the same detailed table
review and call the same services; the Recipe-run route does not duplicate the
source engine or change Authoring navigation.

When the data manager saves the Test target, the target route reads the setup
binding and preselects the union of models declared by the selected Recipe
revisions. `odoo_check_requirements_for_workspace` bulk-reads the exact
protected revisions once and combines their models, fields, Recipe names, and
Recipe-owned Odoo relationship paths in memory. Portable reference tables
remain separate Recipe dependencies. This projection performs no Odoo call
per Recipe.

`GET /projects/{project_id}/runs/{migration_run_id}/odoo` renders the shared
setup evidence as the run-owned **Check Odoo** page. The page presents the
combined requirements as read-only business information and does not render
the Authoring model picker. A copied setup `/schema` URL redirects to this
canonical run URL. A crafted model-scope or generic schema-capture form for
the setup also redirects without changing the Recipe-derived scope. On
**Check this Odoo**, the run-owned Odoo-check `POST` route aligns an older
setup's saved scope with the pinned Recipes and calls the existing bounded
Odoo 19 metadata capture.

The same command uses the [target reader](../../../src/impodo/web/target_readers.py)
to union fields for each related Odoo model, make one combined supporting-value
reader call, and cap every related model at 2,001 returned rows so 2,000 values
is the accepted maximum. It verifies one exact target, reader, access context,
and complete metadata/record fingerprint before saving portable business-key
values. It never saves Odoo numeric IDs. It then calls
`MigrationRunPlanningService.activate_test_run`, which assesses every selected
Recipe, stores the run target and plan atomically, and creates one isolated
application workspace per Recipe. The submitted operation ID is retained on
recoverable errors so an interrupted activation resumes instead of duplicating
work. The former Test activation form and both of its routes are removed;
**Check this Odoo** is the only Test activation command. Ordinary Authoring
schema pages retain their editable model picker and existing service path.
**Fresh data** reads parameter definitions from the exact selected Recipe
revisions in the same bounded bulk read used for source requirements. It asks
for every non-automatic value on the run page. Identical logical parameter IDs
are shown once only when their type, requirement, and constraints agree across
Recipes; a disagreement fails closed. The standard export-as-of value remains
read-only and comes from the Test delivery cutoff.

`TestRunSetupService.replace_fresh_data_run_values` validates submitted values
through the same normalizer used by the Recipe application compiler. The
repository stores normalized Recipe-scoped answers in
`test_run_parameter_values` with an optimistic revision, content hash, stable
actor identity, timestamp, and audit event. A normal run accepts these answers
with the fresh source selection and does not replace them after the Test
DataVersion is frozen. An older frozen delivery may add its missing answers
once. Activation reads this evidence once, adds
the standard export-as-of date where declared, validates every exact Recipe
definition again, and supplies the resulting per-Recipe values to planning.
An older accepted Test delivery with no saved answers stays on **Fresh data**
until its required Recipe values are supplied.

### Planning and collision checks

`review_test_run` verifies one frozen Test package, Project ownership, exact
protected Recipe envelopes, physical source bindings, parameters, controls,
Odoo 19 compatibility, and supporting reference versions. It builds a
deterministic topological order and rejects missing nodes, duplicate edges,
self-dependencies, cycles, and incompatible versions of one named reference
dataset.

The write contract deliberately uses conservative ownership: two
selected Recipes may not both claim the same Odoo model and writable field.
There is no last-writer-wins or reordering escape. General record-domain merge
semantics remain out of scope.

### Shared target evidence

The planner unions all required Odoo models and fields, filters the reviewed
schema to that model set, and stores it once under the run. It similarly
stores only supporting reference datasets named by the selected Recipes.
`RunAwareSchemaRepository` and `RunAwareAdvancedCoverageRepository` project
only one application's requirements into its workspace and reject per-
application recapture.

The Phase 3 Check Odoo command captures one fresh live field snapshot and one
bounded set of current supporting values from the target chosen for the Test
run. It makes no Odoo call per Recipe or source row. The Recipe relationship
paths authorize only their exact related models and business-key fields; the
data manager never selects related tables again. A detected schema change is
kept as a pending candidate and still requires explicit confirmation before
dependent evidence can be replaced. Successful assessment creates the
application workspaces immediately and redirects to the integrated run page.

## Evidence and state

### Provisioning and recovery

The setup operation uses deterministic child identities for the DataVersion,
run, setup workspace, and selection binding. The later activation operation
stores the TargetBinding, requirement plan, target schema, reference bundle,
applications, application workspaces, requirements, and initial issues. One
activation transaction advances the Project revision. Application workspace
stores are created afterward; the activation intent remains pending until
every compiler attempt is recorded.

Replaying either operation after a registry or store fault reconstructs its
stored identities and does not add a data version, run, target binding,
application, or workspace. A changed request under the same operation ID
fails closed.

### Fresh compiler boundary

`RecipeApplicationService` adapts the retained Recipe application compiler to an
already provisioned application workspace. It never calls the superseded
Recipe-owned DataVersion or application-creation paths. It rebuilds governance
and structural preparation, rebinds logical source columns, creates a normal
mapping working draft, and stores a mapping-bound quality seed.

When the current application has no blocker, the same service checks and
submits the freshly rebound mapping through `MappingWorkspaceService`. It does
not copy an Authoring revision or its approval. A fresh validation warning or
invalid result prevents submission and keeps the application blocked. The
portable Recipe's target write contract remains separate from
`approved_write_fields`, which applies only to protected captured-Odoo update
datasets and is empty for normal fresh-file applications.

Portable `NORMALIZE_TEXT` preparation is compiled into the normal scalar
mapping transformation instead of materializing a copied source column.
Source, target, or reference blockers prevent an unsafe mapping. Reviewable
quality-scope or categorical blockers retain a fresh draft but keep the
application `BLOCKED`.

### Run-owned Review and load projection

After Test activation, `start_next_preparation` selects only the first
unreconciled application in the saved order and delegates to the existing
preparation command. It never retries a non-retryable job and never starts a
Production application. `PreparationJobManager` and `LoadJobManager` publish
coarse milestones to `RecipeApplication.status`; their detailed evidence
continues to belong to the workspace services.

`build_integrated_run_review` reads all application identities and issues from
the registry, obtains latest preparation and load snapshots with one in-memory
pass per manager, and builds the ordered cards. It does not open a workspace
database. The status endpoint returns only a view hash and aggregate progress;
it does not reread Recipe definitions. The browser reloads the bounded page
when that projection changes.

A preparation success opens the normal prepared-data review. The existing
preflight and execution routes still own **Check changes**, **Confirm and
load**, and **Verify result**. Comparison records the coarse `COMPARED`
milestone in the run registry. When a complete comparison proposes no writes,
the execution service journals a completed zero-row run without constructing
or calling an Odoo writer. Reconciliation then binds that unchanged result to
the exact comparison before the application becomes `RECONCILED`.

The load worker records `RECONCILED` only after read-back verification has no
unknown outcomes or fallout, then attempts to enqueue the next Recipe. If
automatic enqueue cannot proceed, the run page retains one safe manual action.
A failed or interrupted application remains before its dependants.

## Completion and navigation

The browser redirects to the integrated run page after provisioning. `READY`
means every application has a compatible fresh draft; `BLOCKED` retains the
owning issues and any safe draft. Neither state implies execution,
qualification, rollout selection, or Production authority. The run page links
to the separate qualification review.

Workspace ownership selects one browser journey through
`classify_workspace_journey`. An Authoring workspace keeps the six-stage
navigation. A Test setup workspace exposes only **Fresh data**, **Check Odoo**,
and the activation review. A `RecipeApplication` workspace exposes only the
preparation, review, load, and verification routes grouped under **Review and
load**. `WorkspaceAccessMiddleware` applies this policy after resolving the
verified workspace lineage and before a route opens child state. A stale GET or
POST for an incompatible workspace area redirects to the owning run without
executing that route.

The run page enters the current application through
`GET /projects/{project_id}/runs/{migration_run_id}/applications/{application_id}`.
That route resumes active preparation or load progress, duplicate review,
prepared-data review, comparison, or verification from the latest safe state.
It redirects a downstream application to the run page until its predecessor
is reconciled.
Odoo recovery renders the one shared setup workspace through
`GET /projects/{project_id}/runs/{migration_run_id}/odoo`; the workspace
`/schema` URL redirects there. These run-owned routes prevent application
pages from becoming a second Authoring workflow.
The run enters its source contract through
`GET /projects/{project_id}/test-runs/{migration_run_id}/fresh-data`; workspace
file and table pages are supporting detail rather than another run home.

## Invalidation and recovery

Changing the DataVersion, Recipe selection or revision, dependency graph,
target evidence, reference versions, parameter values, controls, or credential
generation changes plan meaning and requires a new operation. Exact replay may
resume a faulted operation; changed meaning under the same operation ID is
rejected.

## Odoo 19 and performance

The current implementation accepts Odoo 19. Supporting a later Odoo major
version requires extending and testing the compatibility policy. The browser
describes the selected destination as a supported Odoo target so its labels do
not need to change for each newly supported major version.

The exact registry generation is
`impodo-migration-registry-2026-08-project-root`. The DataVersion generation is
`impodo-data-version-store-2026-08-project-owned`; the MigrationWorkspace
reference-store generation is
`impodo-migration-workspace-2026-08-reference-only`. Supported older versions
within those generations upgrade transactionally before use. Other
generations remain unchanged and fail closed.

Provisioning reads one Project outside the application loop and indexes
workspaces by ID. Integrated progress, exact Recipe names, and batch issues use
bounded registry queries. Job managers collect the latest snapshots for all
requested workspace IDs in one pass rather than one scan per card.
Per-application source projection and compiler writes are required
because mutable state is isolated; Odoo calls, Project lookups, and source-row
queries must not scale with Recipe count.

## Code references

| Role | Code |
| --- | --- |
| Domain plan and application state | [`migration_run_planning.py`](../../../src/impodo/migration_run_planning.py) |
| Test setup binding | [`migration_test.py`](../../../src/impodo/migration_test.py) |
| Test setup coordinator | [`TestRunSetupService`](../../../src/impodo/application/run/test_setup_service.py) |
| Stable logical source binding | [`recipe_source_binding.py`](../../../src/impodo/recipe_source_binding.py) |
| Bounded exact Recipe reads | [`RecipeService.read_revisions`](../../../src/impodo/application/recipe/service.py) |
| Planner and provisioning coordinator | [`MigrationRunPlanningService`](../../../src/impodo/application/run/planning_service.py) |
| Fresh Recipe application service | [`RecipeApplicationService`](../../../src/impodo/application/recipe_application_service.py) |
| Run-owned Review and load projection | [`run_review.py`](../../../src/impodo/web/run_review.py) |
| Background preparation summary | [`PreparationJobManager`](../../../src/impodo/application/workspace/preparation/preparation_job_service.py) |
| Background load summary | [`LoadJobManager`](../../../src/impodo/application/workspace/execution/load_jobs.py) |
| Registry and recovery | [`MigrationRunPlanningRepository`](../../../src/impodo/adapters/duckdb/migration_run_planning_repository.py) |
| Test setup persistence | [`TestRunRepository`](../../../src/impodo/adapters/duckdb/test_run_repository.py) |
| Shared Recipe run-value validation | [`recipe_parameters.py`](../../../src/impodo/domain/recipe_parameters.py) |
| Forward-compatible registry schema | [`migration_registry.py`](../../../src/impodo/adapters/duckdb/schema/migration_registry.py) |
| Run-owned schema projection | [`RunAwareSchemaRepository`](../../../src/impodo/adapters/duckdb/run_aware_schema_repository.py) |
| Run-owned reference projection | [`RunAwareAdvancedCoverageRepository`](../../../src/impodo/adapters/duckdb/run_aware_advanced_coverage_repository.py) |
| Browser routes | [`integrated_runs.py`](../../../src/impodo/web/routers/integrated_runs.py) |
| Shared file browser commands | [`source_file_commands.py`](../../../src/impodo/web/source_file_commands.py) |
| Workspace journey policy | [`workspace_journeys.py`](../../../src/impodo/web/workspace_journeys.py) |
| Journey-aware navigation | [`navigation.py`](../../../src/impodo/web/presenters/navigation.py) |

## Verification

- [`tests/test_integrated_recipe_runs.py`](../../../tests/test_integrated_recipe_runs.py)
- [`tests/test_forward_upgrade_compatibility.py`](../../../tests/test_forward_upgrade_compatibility.py)
- [`tests/test_workspace_journeys.py`](../../../tests/test_workspace_journeys.py)
- [`tests/test_project_authoring.py`](../../../tests/test_project_authoring.py)
- [`tests/test_data_version_source_packages.py`](../../../tests/test_data_version_source_packages.py)

## Related documentation

- [Data-manager guide](../../user/guides/integrated-test-runs.md)
- [Project lifecycle](../contracts/project-lifecycle.md)
- [Integrated run lifecycle contract](../contracts/integrated-run-lifecycle.md)
- [Evidence lifecycle](../contracts/evidence-lifecycle.md)
- [Qualification workflow](08-integrated-qualification.md)
