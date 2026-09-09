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

The shared Test and Production **Fresh data** router delegates through
`RunSetupService`. For Test it calls `TestRunSetupService.fresh_data_requirements`. It shows the logical source
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
revisions. `RunOdooRequirementsUseCase.for_workspace` authorizes the
Project, bulk-reads the exact protected revisions once, and combines their
models, fields, Recipe names, and Recipe-owned Odoo relationship paths in
memory. Portable reference tables remain separate Recipe dependencies. This
projection performs no Odoo call per Recipe. `TestRunSetupService` delegates
its stable browser-facing query to this focused use case.

`GET /projects/{project_id}/runs/{migration_run_id}/odoo` renders the shared
setup evidence as the run-owned **Check Odoo** page. The page presents the
combined requirements as read-only business information and does not render
the Authoring model picker. A copied setup `/schema` URL redirects to this
canonical run URL. A crafted model-scope or generic schema-capture form for
the setup also redirects without changing the Recipe-derived scope. On
**Check this Odoo**, the run-owned Odoo-check `POST` route aligns an older
setup's saved scope with the pinned Recipes and calls the existing bounded
Odoo 19 metadata capture.

The same command uses the [target reader](../../../src/impodo/web/composition/target_readers.py)
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
**Fresh data** reads parameter and control definitions from the exact selected Recipe
revisions in the same bounded bulk read used for source requirements. It asks
for every non-automatic value on the run page. Identical logical parameter IDs
are shown once only when their type, requirement, and constraints agree across
Recipes; a disagreement fails closed. The standard export-as-of value remains
read-only and comes from the Test delivery cutoff.

Control prompts stay scoped to one Recipe and retain the table, unit, and
tolerance from that revision. Non-invariant controls require a finite decimal
total from this delivery. Invariant totals come from the Recipe and are
read-only. The shared domain control normalizer validates both Fresh data
answers and compiler inputs, bounds decimal expansion, and rejects a supplied
value that changes an invariant total.

`TestRunSetupService.replace_fresh_data_run_values` validates submitted values
through the same normalizer used by the Recipe application compiler. The
repository stores one `TestRunValues` record containing separate typed
parameter and control answers, plus the selected Recipe revisions and semantic
hashes. It keeps the existing table name
`test_run_parameter_values` with an optimistic revision, content hash, stable
actor identity, timestamp, and audit event. A normal run accepts these answers
with the fresh source selection and does not replace them after the Test
DataVersion is frozen. An older frozen delivery may add its missing answers
once. A legacy record may add missing controls once while preserving its
accepted parameters. Both service validation and the repository transaction
enforce that exception through the same domain rule. Registry schema version
6 permits contract versions 1 and 2 and preserves the exact existing JSON and
content hashes during upgrade. New records use version 2; old records remain
readable without being reinterpreted.

Activation reads this evidence once, adds
the standard export-as-of date where declared, validates every exact Recipe
definition again, and supplies separate parameter and control maps to planning.
The activation request and application binding hash include control values.
Activation retries and required-default recovery retain these saved inputs.
An older accepted Test delivery with no saved answers stays on **Fresh data**
until its required Recipe values are supplied.

### Planning and collision checks

`review_test_run` verifies one frozen Test package, Project ownership, exact
protected Recipe envelopes, physical source bindings, parameters, controls,
Odoo 19 compatibility, and supporting reference versions. It builds a
deterministic topological order and rejects missing nodes, duplicate edges,
self-dependencies, cycles, and incompatible versions of one named reference
dataset.

The review uses the same bulk `RecipeService.read_revisions` operation as
Fresh data. It reads Recipe registry metadata once for the selection while
retaining one protected-envelope verification per selected revision.

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

If categorical coverage blocks an application, the activation route redirects
to `GET /projects/{project_id}/runs/{migration_run_id}/applications/{application_id}/target-matches`
before preparation. `RecipeTargetMatchService.build_review` rescans the application-owned
frozen source snapshots. It obtains Selection choices from the run-projected
schema and Many2one choices from the supporting lookup already captured in the
shared setup workspace. The page therefore makes no new Odoo request. It is
model-independent and supports a scalar Selection source or a Many2one source
with one governed, scope-free business key. It also verifies exact composite
and scoped Many2one keys against the complete ordered captured value. Those
keys remain read-only: the existing mapping contract does not permit explicit
translations for composite or scoped relationships. Missing and ambiguous
keys block confirmation and explain the source, Odoo, or Recipe correction
needed. The focused review does not offer the full mapping editor as recovery.

The focused page collapses choices whose current application mapping still
exists in this target. It renders selectors only for uncovered, missing, or
ambiguous source values. A page with no unresolved values still requires
**Confirm and continue** so the normal mapping check can replace a stale
application blocker. The POST route validates the session and form envelope,
then runs `RecipeTargetMatchService.confirm` in the thread pool. The service
derives the permitted decision keys from a fresh review and rejects unknown
keys, fixed choices, and values outside the current unambiguous choices.
It verifies the working-draft version, mapping hash, and a review hash covering
source evidence, schemas, read identity, and supporting lookup contents before
writing. Stale forms cannot replay answers onto changed rows.

The service checks the compiled Recipe baseline before saving. It changes only
permitted value maps and policies, then calls the existing mapping check,
submission, and application-confirmation services. Validation failures render
on the focused page. Unsupported source domains and uncovered fixed providers
cannot appear as a successful check. Each review collects source coverage once
and reads each distinct supporting lookup once. Suggestion construction indexes
labels and values once per field, avoiding a target-list search for every source
value. These bounds do not establish an overall workflow speedup.

Target-only required fields follow the shared create-field policy before
preparation. Impodo records computed and related fields as Odoo-managed. For a
supported writable field, an exact `default_get` result adds an
application-owned `ODOO_DEFAULT` disposition, so the create request omits that
field and lets the same Odoo context apply its value.

[`decide_verified_create_default`](../../../src/impodo/domain/mapping/create_field_policy.py)
owns the generic risk decision. A verified,
non-company-specific scalar default becomes
`RECIPE_TARGET_ODOO_DEFAULT_HANDLED` information and does not stop the
application. Many2one, Selection, monetary, company-dependent, and
company-scope-unproven defaults become
`RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE` review issues. The shared
[`mapped_target_defaults`](../../../src/impodo/application/run/target_defaults.py)
projection ensures that the confirmation use case and route show the same
review subset. Missing, malformed, or unsupported defaults remain blockers and
require a new Recipe value provider. This policy does not use model or field
names. Its explicit version is part of the application's physical binding hash,
so a later policy change cannot be mistaken for the earlier assessment.

After saving a decision, the route calls `MappingWorkspaceService` to check and
submit the complete mapping, then calls
`RunApplicationRecoveryUseCase.confirm_mapping`. Any other validation error or
warning returns to the full mapping review. A valid application can then enter
the existing preparation scheduler. The Recipe-run navigation presents this
focused page inside **Check Odoo** and keeps **Review and load** unavailable
until confirmation succeeds.

## Evidence and state

### Provisioning and recovery

The setup operation uses deterministic child identities for the DataVersion,
run, setup workspace, and selection binding. The later activation operation
stores the TargetBinding, requirement plan, target schema, reference bundle,
applications, application workspaces, requirements, and initial issues. One
activation transaction advances the Project revision. Application workspace
stores are created afterward; the activation intent remains pending until
every compiler attempt and the required CutoverPlan binding are recorded.
An incomplete compiler attempt retains its workspace draft and leaves
activation pending. A completed mapping is a per-application checkpoint;
retrying activation does not recompile that application or reset later work.

`TestRunSetupService.resume_activation_if_needed` finds the saved operation
by run identity. The Odoo-check command finishes that operation using its
original target evidence before collecting new evidence. The same recovery
also repairs an older committed activation whose CutoverPlan binding is
missing. The repository checks the owning run and original actor before
resuming writes.

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

The seed also retains the compiled mapping baseline. The domain projection
`recipe_mapping_shape` preserves the Recipe's providers, transformations,
identities, relationships, write ownership, and invariant controls. It excludes
permitted categorical choices and control expectations missing from a legacy
compiler baseline. Totals already present in the compiled baseline stay fixed,
including delivery totals collected on Fresh data. The matching form displays
those totals as read-only and preserves their definitions when parsing other
run decisions. The submission guard rejects direct changes to them.
`MappingWorkspaceService.submit_current` checks that projection before
submission. `confirm_mapping` checks it again while rebinding the same quality
rules to the newly confirmed mapping. A full matching draft can therefore be
reviewed without granting permission to change the pinned Recipe's meaning.

Both the web composition and spawned preparation worker supply the Recipe
quality repository to `QualityService`. Missing or stale Recipe seeds block
evaluation. A cached ruleset that an older build published without those rules
is repaired before any row is evaluated. Run-local rule editing retains the
Recipe rules; changing their meaning requires a new Recipe version.

Workspace engine schema version 10 adds the saved mapping baseline through a
structural forward upgrade. Older seeds keep their checks for the exact
original mapping. An older application without a baseline cannot accept new
mapping choices; create a new application of the pinned Recipe in that case.
The upgrade does not infer a baseline from an edited draft.

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

`web/run_commands.py` owns preparation enqueue, retry, recovery, and guarded
run milestone publication. Routes delegate these commands; `web/run_review.py`
only builds the bounded presentation. This separates command ownership from
rendering and avoids a dependency cycle through the preparation router.

After Test activation, `start_next_preparation` selects only the first
unreconciled application in the saved order and delegates to the existing
preparation command. It never retries a non-retryable job and never starts a
Production application. `PreparationJobManager` and `LoadJobManager` publish
coarse milestones to `RecipeApplication.status`; their detailed evidence
continues to belong to the workspace services.

`build_integrated_run_review` reads all application identities and issues from
the registry, obtains latest preparation and load snapshots with one in-memory
pass per manager, and builds the ordered cards. It does not open a workspace
database. The status endpoint returns aggregate progress and compact progress
fields for each application without rereading Recipe definitions. The browser
updates percentages and messages in place. Its structural view hash changes
when application state, actions, or issues change, rather than for each progress
message. Those structural changes still refresh the page.

`application/run/progress.py` owns dependency ordering, durable completion,
attempt relevance, and resume-step selection. A completed session snapshot
does not release the next Recipe before verification is recorded in the run
registry. The run command and direct preparation routes use the same order.
New preparation requests carry their mapping hash. Old attempts cannot advance
a changed mapping or prevent a new preparation request after confirmation.
Saved comparison and verification milestones take precedence over older
preparation snapshots when the application is reopened.

### Recovering published preparation

When a child exits without a terminal notification, the job manager waits for
it to exit and drains its event queue before checking durable evidence once.
`PreparationRecoveryService` validates the requested mapping and current source,
schema, and retention metadata. Its DuckDB repository reads the current
publication headers and their bindings in one query. It does not load row
evidence, rescan source files, or contact Odoo.

A current duplicate evaluation with candidates restores duplicate review.
A current quality and normalization chain restores prepared-data review only
after the bounded preparation session is published. The materialized path,
which has no session row, remains supported. Invalidated or mismatched
publications do not restore completion. Artifact validation remains with the
existing review and execution services.

Entering **Review and load**, continuing the current application, and requesting
preparation can recover that same result after the in-memory job registry is
lost. Only the first unverified application is considered. Active jobs, existing
load progress, blocked applications, and later milestones prevent this read.
An explicit failed or cancelled session result is retained; an unexpected
worker exit may be recovered. Status polling never performs recovery.

Restoration creates a terminal session snapshot without enqueuing a worker.
The command retries milestone publication, including when the worker's earlier
registry notification failed. The registry transaction compares the expected
mapping hash before recording progress. It cannot transfer a result to a
changed mapping. Duplicate evidence is rechecked even when a terminal snapshot
exists, so approving duplicates allows preparation to continue.

Preparation enqueue, retry, and recovery run in the thread pool when called by
async routes. This phase adds no storage schema or durable job history. Once
the session registry is lost, recovery identifies current published work; it
does not reconstruct an interrupted attempt's full history.

Activation and focused target-value GET rendering run outside the async request
thread. This avoids blocking that thread during local compilation or coverage
reads; it is not a claim of faster source processing.

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

Credential reconnection uses the existing captured schema as its authority.
`SchemaWorkspaceService.rebind_current_access` requires equivalent fields,
target, read identity, and context, but does not require Authoring registration
or reopen source data. Recipe application workspaces inherit their schema and
source projection. Initial schema capture retains its registration checks.

## Code references

| Role | Code |
| --- | --- |
| Domain plan and application state | [`migration_run_planning.py`](../../../src/impodo/domain/run/contracts.py) |
| Test setup binding | [`migration_test.py`](../../../src/impodo/domain/run/test_setup.py) |
| Stable Test setup facade | [`TestRunSetupService`](../../../src/impodo/application/run/test_setup_service.py) |
| Restart-safe Test setup creation | [`TestRunSetupStartUseCase`](../../../src/impodo/application/run/test_setup_start.py) |
| Fresh-data values and matching | [`TestRunFreshDataUseCase`](../../../src/impodo/application/run/fresh_data_setup.py) |
| Run-owned Odoo requirement query | [`RunOdooRequirementsUseCase`](../../../src/impodo/application/run/odoo_requirements.py) |
| Stable logical source binding | [`recipe_source_binding.py`](../../../src/impodo/domain/recipe/source_binding.py) |
| Bounded exact Recipe reads | [`RecipeService.read_revisions`](../../../src/impodo/application/recipe/service.py) |
| Stable run-planning facade | [`MigrationRunPlanningService`](../../../src/impodo/application/run/planning_service.py) |
| Test activation | [`TestRunActivationUseCase`](../../../src/impodo/application/run/test_activation.py) |
| Production review and activation | [`ProductionRunReviewUseCase`](../../../src/impodo/application/run/production_review.py) and [`ProductionRunActivationUseCase`](../../../src/impodo/application/run/production_activation.py) |
| Application materialization and recovery | [`RunApplicationMaterializer`](../../../src/impodo/application/run/application_materialization.py) and [`RunApplicationRecoveryUseCase`](../../../src/impodo/application/run/application_recovery.py) |
| Fresh Recipe application service | [`RecipeApplicationService`](../../../src/impodo/application/recipe_application_service.py) |
| Run-owned Review and load projection | [`run_review.py`](../../../src/impodo/web/run_review.py) |
| Preparation commands and guarded run milestones | [`run_commands.py`](../../../src/impodo/web/run_commands.py) |
| Current publication recovery | [`PreparationRecoveryService`](../../../src/impodo/application/workspace/preparation/recovery.py) and [`PreparationRecoveryRepository`](../../../src/impodo/adapters/duckdb/preparation_recovery_repository.py) |
| Application order and resume decisions | [`progress.py`](../../../src/impodo/application/run/progress.py) |
| Immutable Recipe mapping meaning | [`mapping_adaptation.py`](../../../src/impodo/domain/recipe/mapping_adaptation.py) |
| Mapping baseline and business checks | [`RecipeQualitySeedRepository`](../../../src/impodo/adapters/duckdb/recipe_quality_seed_repository.py) |
| Focused target-value review and confirmation | [`target_matches.py`](../../../src/impodo/application/run/target_matches.py) |
| Ordered key and scope serialization shared with capture | [`supporting_lookups.py`](../../../src/impodo/domain/workspace/supporting_lookups.py) |
| Background preparation summary | [`PreparationJobManager`](../../../src/impodo/web/composition/preparation_job_manager.py) |
| Background load summary | [`LoadJobManager`](../../../src/impodo/application/workspace/execution/load_jobs.py) |
| Registry and recovery | [`MigrationRunPlanningRepository`](../../../src/impodo/adapters/duckdb/migration_run_planning_repository.py) |
| Test setup persistence | [`TestRunRepository`](../../../src/impodo/adapters/duckdb/test_run_repository.py) |
| Shared Recipe run-value validation | [`recipe_parameters.py`](../../../src/impodo/domain/recipe_parameters.py) |
| Typed run answers and frozen-answer rules | [`test_setup.py`](../../../src/impodo/domain/run/test_setup.py) |
| Shared control-total validation | [`control_values.py`](../../../src/impodo/domain/recipe/control_values.py) |
| Forward-compatible registry schema | [`migration_registry.py`](../../../src/impodo/adapters/duckdb/schema/migration_registry.py) |
| Run-owned schema projection | [`RunAwareSchemaRepository`](../../../src/impodo/adapters/duckdb/run_aware_schema_repository.py) |
| Run-owned reference projection | [`RunAwareAdvancedCoverageRepository`](../../../src/impodo/adapters/duckdb/run_aware_advanced_coverage_repository.py) |
| Shared setup routes | [`run_fresh_data.py`](../../../src/impodo/web/routers/run_fresh_data.py), [`setup_service.py`](../../../src/impodo/application/run/setup_service.py) |
| Browser routes | [`integrated_runs.py`](../../../src/impodo/web/routers/integrated_runs.py) |
| Shared file browser commands | [`source_file_commands.py`](../../../src/impodo/web/source_file_commands.py) |
| Workspace journey policy | [`workspace_journeys.py`](../../../src/impodo/web/workspace_journeys.py) |
| Journey-aware navigation | [`navigation.py`](../../../src/impodo/web/presenters/navigation.py) |

## Verification

- [`Worker and recovery boundaries`](../../../tests/application/workspace/preparation/test_recovery.py)
- [`Duplicate publication recovery`](../../../tests/integration/duckdb/test_preparation_recovery.py)
- [`Actual worker and browser recovery`](../../../tests/integration/web/test_recipe_preparation_recovery.py)
- [`tests/application/run/test_fresh_data_controls.py`](../../../tests/application/run/test_fresh_data_controls.py)
- [`tests/integration/web/test_fresh_data_controls.py`](../../../tests/integration/web/test_fresh_data_controls.py)
- [`tests/application/run/test_stabilization.py`](../../../tests/application/run/test_stabilization.py)
- [`tests/integration/duckdb/test_recipe_application_evidence.py`](../../../tests/integration/duckdb/test_recipe_application_evidence.py)
- [`tests/application/run/test_odoo_requirements.py`](../../../tests/application/run/test_odoo_requirements.py)
- [`tests/application/run/test_integrated_recipe_runs.py`](../../../tests/application/run/test_integrated_recipe_runs.py)
- [`tests/application/run/test_recipe_target_matches.py`](../../../tests/application/run/test_recipe_target_matches.py)
- [`tests/application/run/test_target_match_service.py`](../../../tests/application/run/test_target_match_service.py)
- [`tests/integration/web/test_recipe_target_matches.py`](../../../tests/integration/web/test_recipe_target_matches.py)
- [`tests/application/run/test_target_defaults.py`](../../../tests/application/run/test_target_defaults.py)
- [`tests/integration/duckdb/test_forward_upgrades.py`](../../../tests/integration/duckdb/test_forward_upgrades.py)
- [`tests/application/workspace/test_journeys.py`](../../../tests/application/workspace/test_journeys.py)
- [Recipe setup navigation and breadcrumbs](../../../tests/application/workspace/test_run_navigation.py)
- [`tests/application/project/test_authoring.py`](../../../tests/application/project/test_authoring.py)
- [`tests/application/data_version/test_source_packages.py`](../../../tests/application/data_version/test_source_packages.py)

## Related documentation

- [Data-manager guide](../../user/guides/integrated-test-runs.md)
- [Project lifecycle](../contracts/project-lifecycle.md)
- [Integrated run lifecycle contract](../contracts/integrated-run-lifecycle.md)
- [Evidence lifecycle](../contracts/evidence-lifecycle.md)
- [Qualification workflow](08-integrated-qualification.md)
