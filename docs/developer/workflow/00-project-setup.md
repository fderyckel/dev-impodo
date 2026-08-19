---
audience: developer
stage: setup
status: current
---

# Recipe and data-version setup

## Responsibility

Recipe setup provisions the aggregate root plus authoring DataVersion 1.
The contained project setup then establishes the evidence and credential
workspace before workflow evidence is created. It owns draft metadata, source
mode, source-file intake, target configuration, governance, registration,
bootstrap deletion, and the data-version overview.

It does not inspect business rows, capture an Odoo schema, or authorize an
Odoo read or write merely because connection details are stored.

## Entry conditions

The local browser session must be authenticated. **New Recipe** creates the
Recipe and a project workspace in `DRAFT`, with revision-checked edits
performed by an identified actor.

## Implementation flow

`projects.py` routes Recipe creation through `RecipeAuthoringService` and the
setup wizard through `ProjectService`. File content is
accepted through the bounded intake service and stored under generated
identifiers. `target.py` handles Local or Remote Odoo configuration and local
stack readiness without expanding the later connector capabilities.

For a published revision, the same router delegates Test DataVersion creation,
focused compatibility review, and application to `RecipeApplicationService`.
That service creates a clean file workspace, stores fresh parameter/control
inputs locally, binds only non-secret live target and credential-generation
evidence, rebuilds supported source preparation and governance, scans current
categorical domains, and saves a normal `MappingWorkingDraft`. It stages
manager-authored quality rules for that exact mapping hash; `QualityService`
regenerates automatic rules after the mapping is confirmed.

After the existing Test preparation, comparison, load, and read-back stages,
`RecipeQualificationService` derives readiness from those exact current
artifacts. The Recipe page requires explicit expected-outcome confirmation to
publish protected qualification evidence, then a separate action to select
that exact qualification as the rollout candidate. A later Recipe revision is
untested and cannot inherit the earlier status.

Registration validates the complete setup, advances the project to
`REGISTERED`, increments its optimistic revision, writes canonical
registration evidence, and records an actor-bound audit event. The overview
then delegates stage status to `build_project_navigation`.

## Code references

| Role | Code |
| --- | --- |
| Recipe creation and publication | [`RecipeAuthoringService`](../../../src/impodo/application/recipe_authoring_service.py) |
| Recipe Test application | [`RecipeApplicationService`](../../../src/impodo/application/recipe_application_service.py) |
| Recipe Test qualification | [`RecipeQualificationService`](../../../src/impodo/application/recipe_qualification_service.py) |
| Application contracts | [`recipe_applications.py`](../../../src/impodo/domain/recipe_applications.py) |
| Contained project lifecycle | [`ProjectService`](../../../src/impodo/projects.py) |
| Registration command | `ProjectService.register` in [`projects.py`](../../../src/impodo/projects.py) |
| Browser routes | [`projects.py`](../../../src/impodo/web/routers/projects.py) |
| Target routes | [`target.py`](../../../src/impodo/web/routers/target.py) |
| Stage projection | `build_project_navigation` in [`navigation.py`](../../../src/impodo/web/presenters/navigation.py) |

## Evidence and state

The durable aggregate root is `Recipe`; `DataVersion` owns one contained
`MigrationProject`. Registration binds the workspace source mode, business
ownership, governance, target identity, source-file catalogue, and revision.
RecipeDraft projects current evidence without copying it. Publication compiles
portable meaning only after the current mapping, governance, quality, and
source evidence pass eligibility. Credentials stay outside Recipe semantics.

## Completion and navigation

An unregistered workspace keeps all six workflow stages locked. A registered
DataVersion obtains an overview and enters either the file-source path or the
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
- [`tests/test_recipe_authoring.py`](../../../tests/test_recipe_authoring.py)
- [`tests/test_recipe_application.py`](../../../tests/test_recipe_application.py)
- [`tests/test_recipe_qualification.py`](../../../tests/test_recipe_qualification.py)
- [`tests/test_recipe_qualification_web.py`](../../../tests/test_recipe_qualification_web.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)

Verify optimistic revisions, registration readiness, contained file paths,
audit behavior, route security, both source modes, and locked-stage rendering.

## Related documentation

- [User guide: Recipe setup](../../user/getting-started.md)
- [Project lifecycle contract](../contracts/project-lifecycle.md)
- [Architecture overview](../../architecture/overview.md)
