# Impodo developer documentation

This is the entry point for developers and technical operators working on
Impodo. Impodo targets Odoo 19 and keeps migration evidence, approval
boundaries, and read-versus-write capabilities explicit.

## Workflow implementation

`MigrationProject` is the browser and domain business root. Each Project owns
its DataVersions and runs. A `MigrationWorkspace` contains current working
evidence, while an optional Project-scoped `Recipe` owns only reusable revision
meaning. Read the [Project lifecycle contract](contracts/project-lifecycle.md)
and [Recipe publication contract](contracts/recipe-lifecycle.md) before
changing identity, persistence, or publication behavior.

Then read [Data project and authoring workspace setup](workflow/00-project-setup.md) and
follow the implemented workspace stages:

1. [Source data](workflow/01-source-data.md)
2. [Odoo data](workflow/02-odoo-data.md)
3. [Match data](workflow/03-match-data.md)
4. [Prepare data](workflow/04-prepare-data.md)
5. [Final review](workflow/05-final-review.md)
6. [Load into Odoo](workflow/06-load-into-odoo.md)

For Project-level reuse after Recipe publication, read
[Integrated multi-Recipe Test runs](workflow/07-integrated-test-runs.md), then
[Integrated Test qualification](workflow/08-integrated-qualification.md), and
[Production rollout with latest data](workflow/09-production-rollout.md).

Each page maps visible behavior to routes, application services, durable
evidence, invalidation rules, and focused tests. The machine-readable ownership
map is [`docs/workflow.yml`](../workflow.yml).

## Normative contracts

Contracts contain only cross-stage required behavior. Workflow pages own the
routes, services, implementation status, performance risks, and focused tests.

- [Optional Recipe publication](contracts/recipe-lifecycle.md)
- [Project and workspace lifecycle](contracts/project-lifecycle.md)
- [Integrated Test run lifecycle](contracts/integrated-run-lifecycle.md)
- [Cutover plan lifecycle](contracts/cutover-plan-lifecycle.md)
- [Production run lifecycle](contracts/production-run-lifecycle.md)
- [Workflow evidence lifecycle](contracts/evidence-lifecycle.md)
- [Canonical staging](contracts/canonical-staging.md)
- [Preflight](contracts/preflight.md)
- [Normalization governance](contracts/normalization.md)
- [Quality and quarantine](contracts/quality-and-quarantine.md)
- [Execution and reconciliation](contracts/execution-and-reconciliation.md)

## CLI, setup, and runbooks

- [Profile authoring](cli/profile-authoring.md)
- [Preflight CLI](cli/preflight.md)
- [Windows development setup](setup/windows.md)
- [Local Odoo technical runbook](runbooks/local-odoo.md)
- [Internal development and release](runbooks/internal-release.md)
- [Remote Odoo 19 acceptance](runbooks/remote-odoo-acceptance.md)
- [Scenario qualification](runbooks/scenario-qualification.md)

## Cross-stage references

- [Architecture overview](../architecture/overview.md)
- [Code organization](../architecture/code-organization.md)
- [Python code map](../architecture/python-code-map.md)
- [Architecture decisions](../decisions/README.md)
- [Acceptance and test strategy](../testing/acceptance.md)
- [Code-organization regression baseline](../testing/code-organization-phase0-baseline.md)
- [Examples and edge cases](reference/examples-and-edge-cases.md)
- [Proposed scalable relationship dependency planning](../plans/scalable-relationship-dependency-planning.md)
- [Documentation style guide](../style-guide.md)

Before changing an Odoo-backed loop, verify that record access is bounded and
batched. New per-row metadata, lookup, write, or read-back calls require an
explicit N+1 review.
