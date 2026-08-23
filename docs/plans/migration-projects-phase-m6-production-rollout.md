# Migration Projects Phase M6 Production rollout

## Status

Completed on 2026-08-23.

M6 implements the fresh latest-data Production use of one exact selected and
qualified Project CutoverPlan. The later M7 clean cutover is recorded
separately.

## Operator outcome

A data manager can now start Production from the Project after selecting an
integrated Test qualification. Impodo first creates a separate setup for the
complete latest delivery and current Production Odoo 19 access. Only after
those checks pass does Impodo create the Recipe application workspaces.

The qualified plan supplies tested reusable rules and order. It does not
supply Production data, credentials, comparison, approval, load, or
reconciliation evidence.

## Implemented boundary

### Fresh Production setup

One request creates four distinct identities:

- a fresh Project-owned Production DataVersion;
- a fresh Production MigrationRun;
- a setup MigrationWorkspace for data and target review; and
- a setup-only ProductionRunBinding pinned to the selected qualification and
  exact CutoverPlan revision.

The source package begins empty and draft. The existing file acceptance
workflow freezes the complete latest package. M6 rejects Odoo-source plans
early because the current Odoo-source product path has no safe round-trip
write workflow. Setup has no target binding and grants no Odoo write authority.

### Exact activation

Activation rechecks the authenticated qualification, selected plan, Recipe
semantic requirements, dependencies, write ownership, current source
bindings, required values, controls, Odoo fields, supporting lists, and target
identity. The Production target must differ from the qualified Test target.

Read and write credentials use separate target-and-role vault entries and
separate random generations. The browser also rejects the same submitted
secret for both roles. The write key receives one bounded identity and model
permission probe without writing a record.

### Reused engine, fresh evidence

Production uses the M4 run planner, union requirement plan, filtered run
schema/reference capture, source projection, compiler, and six-stage workspace
engine. It does not add a second execution path.

Each selected Recipe gets a new RecipeApplication and isolated
MigrationWorkspace. Application workspaces reuse immutable DataVersion dataset
references and one run-owned target capture. They copy no source tables,
credential, mapping draft, comparison, approval, execution journal, read-back,
or reconciliation result from Test.

### Write-time authority

Before the browser constructs a Production writer, M6 verifies the current
rollout selection, exact plan hash, authenticated qualification, frozen
Production DataVersion, target, read identity and generation, and write
identity and generation. Credential rotation or target/context drift stops the
write and asks for fresh review. A replacement key for the same evidenced
identity may continue after a fresh comparison and probe; changed identity or
context requires a new setup. Existing dependency and unknown-outcome guards
remain in force.

### Persistence and recovery

The exact registry generation is
`impodo-migration-registry-2026-08-m6`. M5 and older development storage are
rejected rather than upgraded.

Setup binding and activation use separate restart-safe intents. Activation
commits registry identities first, creates application stores second, and
commits compiler results last. Exact retry after a cross-store fault resumes
the stored activation meaning. Changed target, credentials, parameters, or
controls under that operation identity fail closed.

## Blind spots closed during implementation

- A Production setup workspace and an application workspace now resolve one
  run-owned credential vault entry. Secrets are not copied per Recipe.
- Project overview resolves the Authoring workspace by DataVersion purpose, so
  the later Production setup workspace cannot become the Recipe publication
  source by ordering accident.
- Project and credential status use bounded registry queries rather than
  opening every application workspace.
- Exact activation recovery now continues application-store creation and
  materialization after a registry commit fault instead of returning a
  misleading draft-ready bundle.
- The operator-facing run page distinguishes qualified reusable meaning from
  fresh Production evidence.
- The same read secret cannot be submitted as the Production write secret.

## Verification

Focused M6 acceptance is in
`tests/test_migration_project_phase_m6_production_rollout.py`. It covers:

- fresh distinct Production data, run, and setup identities;
- restart-safe setup replay;
- latest package identity distinct from Test;
- a different compatible Odoo 19 target;
- exact plan and Recipe revision pins;
- two isolated application workspaces;
- stale credential-generation rejection and same-identity rotation after a
  fresh comparison;
- Test-target reuse rejection;
- browser explanation of Test/Production separation; and
- recovery after registry commit but before application-store creation.

M4 and M5 focused tests remain the regression gates for the shared compiler,
Test plan, qualification, and selection behavior. Storage rejection tests own
the clean development reset path.

## Documentation cutover

This change updates the active implementation plan, roadmap, ADR-014 status,
documentation index, user and developer indexes, workflow registry, security
boundary, paired Production workflow pages, Production lifecycle contract,
and current BPMN model. Historical Recipe-first reports remain labelled
point-in-time evidence. M7 later removed their superseded active code and
fixtures.

## Historical handoff

M7 removed Recipe-first ownership, compatibility services, aliases, migrations,
routes, templates, tests, and stale active documentation while retaining the
Project-first M0-M6 behavior. See the [M7 clean-cutover
record](migration-projects-phase-m7-clean-cutover.md).
