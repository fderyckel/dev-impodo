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
unpublished Recipe deletion, and the data-version overview.

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

Registered Authoring workspaces may also store explicit custom parameter
declarations through `RecipeAuthoringService`. The project-local,
content-hashed declaration set is separate from every Test or Production value
set. Publication converts stable names such as `warehouse` to logical IDs such
as `parameter:warehouse`; file Recipes also receive the built-in export as-of
date. Declarations support string, date, integer, and decimal values and are
limited to controls/provenance use sites. Application rejects values whose
logical IDs are absent from the selected immutable revision.

For a published revision, the same router delegates Test and Production
DataVersion creation, focused compatibility review, and application to
`RecipeApplicationService`. Test creation pins the current revision. Production
creation requires the current cutover candidate and pins its exact qualified
revision even when a newer revision exists. Both create clean file workspaces,
store fresh parameter/control inputs locally, bind only non-secret live target
and credential-generation evidence, rebuild supported source preparation and
governance, scan current categorical domains, and save a normal
`MappingWorkingDraft`. Manager-authored quality rules are staged for that exact
mapping hash; `QualityService` regenerates automatic rules after the mapping is
confirmed.

After the existing Test preparation, comparison, load, and read-back stages,
`RecipeQualificationService` derives readiness from those exact current
artifacts. The Recipe page requires explicit expected-outcome confirmation to
publish protected qualification evidence, then a separate action to select
that exact qualification as the rollout candidate. A later Recipe revision is
untested and cannot inherit the earlier status.

**Run with latest data** creates the Production DataVersion without copying
Test source, target, mapping, preparation, quality, comparison, approval,
execution, reconciliation, or credential evidence. Its TargetBinding is
explicitly Production and requires a fresh read credential and live probe. The
existing load boundary separately probes the current write credential against
the exact Production target, Odoo context, write scope, and execution snapshot.

Registration validates the complete setup, advances the project to
`REGISTERED`, increments its optimistic revision, writes canonical
registration evidence, and records an actor-bound audit event. The overview
then delegates stage status to `build_project_navigation`.

## Code references

| Role | Code |
| --- | --- |
| Recipe creation and publication | [`RecipeAuthoringService`](../../../src/impodo/application/recipe_authoring_service.py) |
| Authoring parameter contracts | [`recipe_parameters.py`](../../../src/impodo/domain/recipe_parameters.py) |
| Authoring parameter persistence | [`RecipeAuthoringRepository`](../../../src/impodo/adapters/duckdb/recipe_authoring_repository.py) |
| Recipe Test and Production application | [`RecipeApplicationService`](../../../src/impodo/application/recipe_application_service.py) |
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
RecipeDraft projects current Authoring evidence without copying it. Publication
compiles portable meaning only after the current mapping, governance, quality,
source, and parameter-declaration evidence pass eligibility. Parameter
declarations are Recipe meaning; confirmed parameter and control values are
DataVersion evidence. Test and Production DataVersions cannot publish new
Recipe meaning. Credentials stay outside Recipe semantics.

## Completion and navigation

An unregistered workspace keeps all six workflow stages locked. A registered
DataVersion obtains an overview and enters either the file-source path or the
Odoo-source path. Navigation is a bounded read projection and must not mutate
state or contact Odoo.

## Invalidation and recovery

All setup commands require the expected project revision. A stale form fails
closed and must be reloaded. Registered file projects may add or remove a file
only until the first source selection exists; later correction requires a new
data version. Unpublished deletion goes through the Recipe aggregate with exact
Recipe and workspace revisions, not direct filesystem removal.

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
- [`tests/test_recipe_representative_shapes.py`](../../../tests/test_recipe_representative_shapes.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)

Verify optimistic revisions, registration readiness, contained file paths,
audit behavior, route security, both source modes, and locked-stage rendering.

## Related documentation

- [User guide: Recipe setup](../../user/getting-started.md)
- [Project lifecycle contract](../contracts/project-lifecycle.md)
- [Architecture overview](../../architecture/overview.md)
