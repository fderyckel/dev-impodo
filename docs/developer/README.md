# Impodo developer documentation

This is the entry point for developers and technical operators working on
Impodo. Impodo targets Odoo 19 and keeps migration evidence, approval
boundaries, and read-versus-write capabilities explicit.

## Workflow implementation

The browser calls the reusable business object a **project**; the domain calls
that aggregate `Recipe`. Each `DataVersion` owns one internal
workspace, represented in the current browser code by `WorkspaceState`. Read the
[Recipe lifecycle contract](contracts/recipe-lifecycle.md) before changing
identity, persistence, application, or qualification behavior.

Then read [Recipe and data-version setup](workflow/00-project-setup.md) and
follow the implemented workspace stages:

1. [Source data](workflow/01-source-data.md)
2. [Odoo data](workflow/02-odoo-data.md)
3. [Match data](workflow/03-match-data.md)
4. [Prepare data](workflow/04-prepare-data.md)
5. [Final review](workflow/05-final-review.md)
6. [Load into Odoo](workflow/06-load-into-odoo.md)

Each page maps visible behavior to routes, application services, durable
evidence, invalidation rules, and focused tests. The machine-readable ownership
map is [`docs/workflow.yml`](../workflow.yml).

## Normative contracts

Contracts contain only cross-stage required behavior. Workflow pages own the
routes, services, implementation status, performance risks, and focused tests.

- [Recipe and data-version lifecycle](contracts/recipe-lifecycle.md)
- [Contained workspace lifecycle](contracts/project-lifecycle.md)
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

## Cross-stage references

- [Architecture overview](../architecture/overview.md)
- [Python code map](../architecture/python-code-map.md)
- [Architecture decisions](../decisions/README.md)
- [Acceptance and test strategy](../testing/acceptance.md)
- [Examples and edge cases](reference/examples-and-edge-cases.md)
- [Documentation style guide](../style-guide.md)

Before changing an Odoo-backed loop, verify that record access is bounded and
batched. New per-row metadata, lookup, write, or read-back calls require an
explicit N+1 review.
