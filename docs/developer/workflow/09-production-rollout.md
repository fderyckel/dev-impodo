---
audience: developer
stage: production-rollout
status: current
---

# Production rollout with latest data

## Responsibility

Production rollout applies one current selected and authenticated CutoverPlan revision to a
fresh complete Production DataVersion and different compatible Odoo 19 target.
It creates fresh isolated application workspaces through the existing compiler
and execution engine. Test contributes reusable qualified meaning only.

## Entry conditions

The Project must own a current rollout selection for an exact qualified
CutoverPlan revision. Its qualified Test run, target binding, and protected
evidence must still authenticate. The actor needs Production setup and
activation capabilities.

## Implementation flow

### Setup without authority

`ProductionCutoverService.start_setup` revalidates the current selection,
qualification artifact, plan hash, and qualified Test source evidence. One
restart-safe parent request creates a fresh Production DataVersion, draft
source package, Production MigrationRun, and setup MigrationWorkspace. A
`ProductionRunBinding` records their exact selection lineage in `SETUP` state.
No target binding or write credential generation exists yet.

The normal source workflow accepts the complete latest delivery into the new
DataVersion. The normal Odoo-data workflow captures current Production schema,
references, read identity, and read credential generation in the setup
workspace. Activation fails early for an Odoo-source CutoverPlan because the current
Odoo-source product path stops after capture and cannot safely round-trip to
Production.

### Exact activation

`ProductionCutoverService.activate` revalidates selection and protected
qualification, reads the qualified Test target identity, and delegates to
`MigrationRunPlanningService.activate_production_run`. Its
`ProductionRunValuesUseCase` reads the pinned Recipe revisions in one batch
and uses the same typed prompts and canonical parameter/control validators
as Test. The form binds its answers to the exact plan and delivery. Invalid
answers and stale forms are rejected before the write probe or vault update.
Failed forms retain editable answers without redisplaying secrets. Review uses the exact
plan revisions and dependency graph. It recompiles current physical source
bindings, parameters, controls, Odoo requirements, references, and write
claims without creating an application.

The review blocks incomplete source coverage, semantic requirement drift,
changed write ownership, dependency or collision errors, incomplete shared
controls, incompatible Odoo evidence, and reuse of the qualified Test target.
The browser performs one bounded write-identity probe. It rejects the same API
secret for read and write roles while storing no secret-derived equality
evidence.

After review, `MigrationRunPlanningRepository.activate_production_run`
atomically adds the run target, union requirement plan, run schema/reference
capture, Recipe applications, application workspaces, and active Production
binding. The repository then creates each workspace store. The shared
`RunApplicationMaterializer` selects its logical datasets, provisions a fresh
engine state, and invokes the existing compiler. Activation and rendering run
in the thread pool so compilation does not block the async request loop.

## Evidence and state

The active binding retains non-secret hashes for target, read and write
credential generations, write principal, observed permissions, company and
language context, delivery parameters, controls, and complete activation
evidence. Credentials remain in the setup workspace vault; application
workspaces resolve that one run-owned vault owner without copying secrets.

Every Production application starts with a fresh mapping draft and no
comparison, approval, execution, read-back, or reconciliation evidence. The
run page labels the plan as qualified meaning selected for rollout and does not
offer a second Production qualification action.

## Completion and navigation

The Project overview offers **Start Production setup** after rollout selection.
Setup first navigates to **Fresh data**, then **Check Odoo**. **Continue
Production setup** opens the activation review. After activation, the
Production run page lists applications in the qualified dependency order and
enters each one through **Review and load**. Authoring retains the normal six
stages; Production setup and application workspaces cannot expose that
Authoring journey.

`ACTIVE` records the registry's target authority and may precede completed
compilation. The activation operation must be `COMMITTED` before the browser
calls setup complete or opens its Review and load page. Preparation commands
and the execution authority guard enforce this boundary too. The Project
overview distinguishes setup completion from `MigrationRunState.COMPLETED`.
The Production run page resolves its plan through `ProductionRunBinding`;
it does not require a Test run binding.

## Invalidation and recovery

Before writer construction, `ProductionCutoverService.assert_execution_authority`
checks the current selection, plan hash, authenticated qualification, frozen
DataVersion, run target, read and write identities, and both credential
generations. A rotated read key stops a stale comparison. The same evidenced
identity may continue after a fresh comparison and write probe; changed
principal, permissions, or context requires a new Production setup. The
existing dependency guard still stops downstream applications until
predecessors reconcile.

Activation has one registry transaction followed by application-store creation
and compiler materialization. The versioned `activation_inputs` payload in
its existing operation intent stores canonical answers and the observed write
identity, with no API secret. `resume_activation` uses that payload and the
saved schema, references, generations, operation ID, and expected revision.
The original actor may resume before or after registry commit; mapped work
areas are reused. A competing operation for the same run is rejected.

`POST .../activate/resume` requires the authenticated session and CSRF token.
It completes local setup without another write probe or credential-store
change. Current selection, plan, qualification, and Recipe meaning are
revalidated. Later comparison and execution still require current credentials.
Completed replay returns the same run. Older pending intents without saved
inputs retain their history and offer a new setup instead of an unusable retry.
Reconciliation remains available after an unknown write outcome so an operator
can establish what happened before retry.

## Odoo 19 and performance

The exact registry generation is
`impodo-migration-registry-2026-08-project-root`. Supported older versions in
that generation upgrade transactionally before use. Other generations remain
unchanged and fail closed.

Project overview loads Production bindings with one registry query and setup
completion with one additional query that excludes intent payloads. Credential
owner resolution is one joined registry query. Activation captures one
run-level filtered schema and reference bundle, then projects them to
applications. It performs no source copy, target recapture per Recipe, Odoo
call per source row, or N+1 workspace open for Project status.

## Code references

| Role | Code |
| --- | --- |
| Domain binding | [`migration_production.py`](../../../src/impodo/domain/run/production.py) |
| Setup and authority guard | [`production_cutover_service.py`](../../../src/impodo/application/production_cutover_service.py) |
| Typed Production answers | [`production_values.py`](../../../src/impodo/application/run/production_values.py) |
| Shared Test and Production prompts | [`fresh_data_values.py`](../../../src/impodo/application/run/fresh_data_values.py), [`_run_value_fields.html`](../../../src/impodo/web/templates/_run_value_fields.html), [`_run_control_fields.html`](../../../src/impodo/web/templates/_run_control_fields.html) |
| Shared review and compiler path | [`planning_service.py`](../../../src/impodo/application/run/planning_service.py) |
| Production registry binding | [`production_run_repository.py`](../../../src/impodo/adapters/duckdb/production_run_repository.py) |
| Run activation and recovery | [`migration_run_planning_repository.py`](../../../src/impodo/adapters/duckdb/migration_run_planning_repository.py) |
| Browser workflow | [`production_runs.py`](../../../src/impodo/web/routers/production_runs.py), [`execution.py`](../../../src/impodo/web/routers/execution.py), [`preflight.py`](../../../src/impodo/web/routers/preflight.py) |

## Verification

- [`tests/application/run/test_production_rollout.py`](../../../tests/application/run/test_production_rollout.py)
- [`Typed values and stale evidence`](../../../tests/application/run/test_production_values.py)
- [`Production browser compilation and restart recovery`](../../../tests/integration/web/test_production_readiness.py)

The focused gate proves fresh setup identity, exact plan pins, a different
Production target, separate credential generations, isolated application
workspaces, stale credential-generation rejection, same-identity rotation
after fresh comparison, target-reuse rejection, browser separation language,
and recovery after a registry/store boundary fault.

The browser journey uses real qualification publication, Recipe compilation,
source acceptance, and preparation. Its Production total differs from Test.
It substitutes Odoo metadata, the write probe, and completed Test execution
evidence. It therefore does not establish live Odoo qualification-to-load
acceptance or representative performance.

## Current limitations

Production still uses the generic source confirmation and Odoo capture pages.
Its upload and logical table matching do not yet share the complete guided
Test Fresh data flow. Unsubmitted Production answers are retained on form
errors but are not a durable draft. Large-plan setup compilation remains
synchronous within its worker thread; no background activation queue or
whole-workflow timing improvement is claimed.

## Related documentation

- [Data-manager guide](../../user/guides/production-rollout.md)
- [Production run lifecycle contract](../contracts/production-run-lifecycle.md)
- [Cutover plan lifecycle contract](../contracts/cutover-plan-lifecycle.md)
- [Execution and reconciliation contract](../contracts/execution-and-reconciliation.md)
