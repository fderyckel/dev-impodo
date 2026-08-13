---
audience: developer
stage: setup
status: current
---

# Project setup

## Responsibility

Project setup establishes the governed migration boundary before workflow
evidence is created. It owns draft metadata, source mode, source-file intake,
target configuration, governance, registration, deletion, and the project
overview.

It does not inspect business rows, capture an Odoo schema, or authorize an
Odoo read or write merely because connection details are stored.

## Entry conditions

The local browser session must be authenticated. New projects begin in
`DRAFT`, with revision-checked edits performed by an identified actor.

## Implementation flow

`projects.py` routes the setup wizard through `ProjectService`. File content is
accepted through the bounded intake service and stored under generated
identifiers. `target.py` handles Local or Remote Odoo configuration and local
stack readiness without expanding the later connector capabilities.

Registration validates the complete setup, advances the project to
`REGISTERED`, increments its optimistic revision, writes canonical
registration evidence, and records an actor-bound audit event. The overview
then delegates stage status to `build_project_navigation`.

## Code references

| Role | Code |
| --- | --- |
| Project lifecycle | [`ProjectService`](../../../src/impodo/projects.py) |
| Registration command | `ProjectService.register` in [`projects.py`](../../../src/impodo/projects.py) |
| Browser routes | [`projects.py`](../../../src/impodo/web/routers/projects.py) |
| Target routes | [`target.py`](../../../src/impodo/web/routers/target.py) |
| Stage projection | `build_project_navigation` in [`navigation.py`](../../../src/impodo/web/presenters/navigation.py) |

## Evidence and state

The durable aggregate is `MigrationProject`. Registration binds its source
mode, business ownership, governance, target identity, source-file catalogue,
and revision. Audit entries retain the actor and transition. Credentials stay
outside the project aggregate and do not become portable evidence.

## Completion and navigation

An unregistered project keeps all six workflow stages locked. A registered
project obtains an overview and enters either the file-source path or the
Odoo-source path. Navigation is a bounded read projection and must not mutate
state or contact Odoo.

## Invalidation and recovery

All setup commands require the expected project revision. A stale form fails
closed and must be reloaded. Registered file projects may add or remove a file
only until the first source selection exists; later correction requires a new
project. Deletion goes through `ProjectService`, not direct filesystem removal.

## Odoo 19 and performance

Connection configuration is not an API entitlement check and is not write
authorization. Keep readiness probes bounded and capability-specific. The
overview and navigation must use request-scoped projections; do not introduce
per-stage or per-dataset repository reads in a loop.

## Verification

- [`tests/test_projects.py`](../../../tests/test_projects.py)
- [`tests/test_project_security.py`](../../../tests/test_project_security.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)

Verify optimistic revisions, registration readiness, contained file paths,
audit behavior, route security, both source modes, and locked-stage rendering.

## Related documentation

- [User guide: Project setup](../../user/getting-started.md)
- [Migration project contract](../../contracts/01-migration-project.md)
- [Architecture overview](../../architecture/overview.md)
