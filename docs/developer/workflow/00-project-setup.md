---
audience: developer
stage: setup
status: current
---

# Recipe and data-version setup

## Responsibility

Recipe setup provisions the aggregate root plus authoring DataVersion 1.
The contained project setup then establishes the evidence and credential
workspace before workflow evidence is created. The normal browser path owns the
operator project name, source mode, initial source-file intake or Odoo-source
connection, registration, unpublished Recipe deletion, and the data-version
overview. File-source destination configuration is deliberately deferred to
the Odoo-data stage.

It does not inspect business rows, capture an Odoo schema, or authorize an
Odoo read or write merely because connection details are stored.

## Entry conditions

The local browser session must be authenticated. **New project** creates the
Recipe, Authoring DataVersion 1, and a contained workspace in `DRAFT`, with
revision-checked edits performed by an identified actor. “Project” is the
operator label; `Recipe` remains the domain aggregate root.

## Implementation flow

`projects.py` routes operator-project creation through
`RecipeAuthoringService` and the contained workspace through `ProjectService`.
The first form asks only for name and source mode. File content is accepted in
one or more uploads through the bounded intake service and stored under
generated identifiers; **Use these files and continue** registers that
workspace. For Odoo source mode, `target.py` performs a purpose-specific
read-only connection check and registers before schema discovery. The former
details, governance, and confirmation pages are compatibility surfaces, not the
normal setup sequence.

For a registered file workspace, `target.py` is reached later from Odoo data to
configure the Local or Remote destination. `OdooConnectionTestService`
identifies the exact Odoo 19 database and verifies the relevant read identity
without repeating model or field discovery; schema capture remains the next
stage.

Registered Authoring workspaces may also store explicit custom parameter
declarations through `RecipeAuthoringService`. The project-local,
content-hashed declaration set is separate from every Test or Production value
set. Publication converts stable names such as `warehouse` to logical IDs such
as `parameter:warehouse`; file Recipes also receive the built-in export as-of
date. Declarations support string, date, integer, and decimal values and are
limited to controls/provenance use sites. Application rejects values whose
logical IDs are absent from the selected immutable revision.

For a published revision, the Recipe routes delegate Test and Production
DataVersion creation, focused compatibility review, and application to
`RecipeApplicationService`. Test creation pins the current revision. Production
creation requires the current cutover candidate and pins its exact qualified
revision even when a newer revision exists. Both create clean file workspaces
and first collect fresh source plus parameter/control input. Their remote Odoo
destination is configured later in Odoo data; accepted application then binds
only non-secret live target and credential-generation evidence, rebuilds
supported source preparation and governance, scans current categorical
domains, and saves a normal `MappingWorkingDraft`. Manager-authored quality
rules are staged for that exact mapping hash; `QualityService` regenerates
automatic rules after the mapping is confirmed.

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

Registration validates the mode-specific source boundary, advances the
workspace to `REGISTERED`, increments its optimistic revision, writes canonical
registration evidence, and records an actor-bound audit event. File mode needs
at least one source file but no destination. Odoo source mode needs exact
connection identity and a successful browser check. The workflow then enters
source inspection/capture directly and delegates stage status to
`build_project_navigation`.

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
| Purpose-specific connection check | [`OdooConnectionTestService`](../../../src/impodo/application/odoo_connection_service.py) |
| Stage projection | `build_project_navigation` in [`navigation.py`](../../../src/impodo/web/presenters/navigation.py) |

## Evidence and state

The durable aggregate root is `Recipe`; `DataVersion` owns one contained
`MigrationProject`. Registration binds the workspace source mode, mode-specific
source setup, and revision. Target identity is later DataVersion evidence for
file mode and initial source evidence for Odoo mode. RecipeDraft projects
current Authoring evidence without copying it. Publication compiles portable
meaning only after the current mapping, governance, quality, source, and
parameter-declaration evidence pass eligibility. Parameter declarations are
Recipe meaning; confirmed parameter and control values are DataVersion
evidence. Test and Production DataVersions cannot publish new Recipe meaning.
Credentials stay outside Recipe semantics.

## Completion and navigation

An unregistered workspace keeps all six workflow stages locked. File setup
continues directly to Source data; Odoo-source setup continues to schema
capture because eligible schema defines the bounded capture. A registered
DataVersion obtains an overview and follows the appropriate source path.
Navigation is a bounded read projection and must not mutate state or contact
Odoo.

## Invalidation and recovery

All setup commands require the expected workspace revision. A stale form fails
closed and must be reloaded. Registered file workspaces may add or remove a
file only until the first source selection exists; later correction requires a
new DataVersion. Changing the file workspace's later Odoo destination
invalidates its target-derived evidence. Unpublished deletion goes through the
Recipe aggregate with exact Recipe and workspace revisions, not direct
filesystem removal.

## Odoo 19 and performance

Connection checking is not schema discovery and is not write authorization.
Keep readiness probes bounded and purpose-specific; model and field capture
belongs to Odoo data and must not be repeated during a connection check. The
overview and navigation must use request-scoped projections; do not introduce
per-stage or per-dataset repository reads in a loop.

## Verification

- [`tests/test_projects.py`](../../../tests/test_projects.py)
- [`tests/test_project_security.py`](../../../tests/test_project_security.py)
- [`tests/test_recipe_authoring.py`](../../../tests/test_recipe_authoring.py)
- [`tests/test_recipe_application.py`](../../../tests/test_recipe_application.py)
- [`tests/test_recipe_qualification.py`](../../../tests/test_recipe_qualification.py)
- [`tests/test_recipe_qualification_web.py`](../../../tests/test_recipe_qualification_web.py)
- [`tests/test_odoo_connection_service.py`](../../../tests/test_odoo_connection_service.py)
- [`tests/test_recipe_representative_shapes.py`](../../../tests/test_recipe_representative_shapes.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)

Verify optimistic revisions, registration readiness, contained file paths,
audit behavior, route security, both source modes, and locked-stage rendering.

## Related documentation

- [User guide: Recipe setup](../../user/getting-started.md)
- [Recipe and data-version lifecycle contract](../contracts/recipe-lifecycle.md)
- [Project lifecycle contract](../contracts/project-lifecycle.md)
- [Architecture overview](../../architecture/overview.md)
