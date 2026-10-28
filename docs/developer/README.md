# Impodo developer documentation

This is the entry point for developers and technical operators working on
Impodo. Impodo supports Odoo 19 and final Odoo 20 reads, Recipe authoring,
writes, recovery, and verification. Odoo 20 Production remains disabled.
Migration evidence, approval boundaries, and read-versus-write capabilities
stay explicit.

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
- [Local worktree containment](runbooks/local-worktree-containment.md)
- [Local Odoo technical runbook](runbooks/local-odoo.md)
- [Internal development and release](runbooks/internal-release.md)
- [Remote Odoo 19 acceptance](runbooks/remote-odoo-acceptance.md)
- [Scenario qualification](runbooks/scenario-qualification.md)

## Python code quality ownership

The [Python code quality reference](reference/python-code-quality.md) defines
the ownership boundary between Ruff, Basedpyright, runtime contracts, and
focused tests. It also defines the baseline policy, narrow exception rules,
and deliberate tool-upgrade process.

Run the pinned repository quality gate from the repository root:

```powershell
uv run --frozen --group quality python scripts/check_python_quality.py
```

## Cross-stage references

- [Workflow responsiveness qualification](../plans/responsive-workflow-liveness-and-navigation.md)
- [Architecture overview](../architecture/overview.md)
- [Code organization](../architecture/code-organization.md)
- [Local browser database operation scopes](../architecture/code-organization.md#local-browser-database-operation-scopes)
- [Python code map](../architecture/python-code-map.md)
- [Architecture decisions](../decisions/README.md)
- [Platform support and testing](reference/platform-support-and-testing.md)
- [Examples and edge cases](reference/examples-and-edge-cases.md)
- [Documentation style guide](../style-guide.md)

Before changing an Odoo-backed loop, verify that record access is bounded and
batched. New per-row metadata, lookup, write, or read-back calls require an
explicit N+1 review.
