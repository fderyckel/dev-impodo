# Impodo developer documentation

This is the entry point for developers changing the Impodo browser workflow.
Impodo targets Odoo 19 and keeps migration evidence, approval boundaries, and
read-versus-write capabilities explicit.

## Workflow implementation

Read [Project setup](workflow/00-project-setup.md), then follow the implemented
browser stages:

1. [Source data](workflow/01-source-data.md)
2. [Odoo data](workflow/02-odoo-data.md)
3. [Match data](workflow/03-match-data.md)
4. [Prepare data](workflow/04-prepare-data.md)
5. [Final review](workflow/05-final-review.md)
6. [Load into Odoo](workflow/06-load-into-odoo.md)

Each page maps visible behavior to routes, application services, durable
evidence, invalidation rules, and focused tests. The machine-readable ownership
map is [`docs/workflow.yml`](../workflow.yml).

## Cross-stage references

- [Architecture overview](../architecture/overview.md)
- [Python code map](../architecture/python-code-map.md)
- [Architecture decisions](../decisions/README.md)
- [Migration project contract](../contracts/01-migration-project.md)
- [Browser workspace contract](../contracts/02-workspace.md)
- [Acceptance and test strategy](../testing/acceptance.md)
- [Documentation style guide](../style-guide.md)

Before changing an Odoo-backed loop, verify that record access is bounded and
batched. New per-row metadata, lookup, write, or read-back calls require an
explicit N+1 review.
