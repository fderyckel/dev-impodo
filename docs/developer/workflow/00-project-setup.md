---
audience: developer
stage: setup
status: current
---

# Recipe and data-version setup

## Responsibility

Recipe setup creates the `Recipe` aggregate root and Authoring DataVersion 1.
It also creates a contained workspace for that DataVersion before any workflow
evidence exists. The normal browser path records the operator's project name
and source mode. It then accepts the initial source files or verifies the
Odoo-source connection, registers the workspace, presents the DataVersion
overview, and allows deletion while the Recipe remains unpublished. A
file-source workspace configures its Odoo destination later in **Odoo data**.

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
The first form asks only for the project name and source mode. For file mode,
the bounded intake service accepts one or more uploads and stores them under
generated identifiers. Choosing **Use these files and continue** then
registers the workspace. For Odoo source mode, `target.py` performs a
purpose-specific read-only connection check and registers the workspace before
schema discovery. The former details, governance, and confirmation pages remain
compatibility surfaces; they are not part of the normal setup sequence.

For a registered file workspace, `target.py` is reached later from Odoo data to
configure the Local or Remote destination. `OdooConnectionTestService`
identifies the exact Odoo 19 database and verifies the relevant read identity
without repeating model or field discovery; schema capture remains the next
stage.

Registered Authoring workspaces can store explicit custom parameter
declarations through `RecipeAuthoringService`. Impodo hashes this project-local
declaration set independently from the values supplied by any Test or
Production DataVersion. During publication, Impodo converts a stable name such
as `warehouse` into a logical ID such as `parameter:warehouse`. A file-based
Recipe also receives the built-in export as-of date. Declarations can use
string, date, integer, or decimal values, and only control or provenance sites
may consume them. Application rejects a value when the selected immutable
revision does not declare its logical ID.

For a published revision, the Recipe routes delegate Test and Production
DataVersion creation, compatibility review, and application to
`RecipeApplicationService`. Creating a Test DataVersion pins the current
published revision. Creating a Production DataVersion requires the current
cutover candidate and pins that candidate's exact qualified revision, even
when the Recipe has a newer revision.

Both actions create clean file workspaces. The data manager must supply fresh
source data, parameter values, and control values. The remote Odoo destination
is configured later in **Odoo data**. When application succeeds, Impodo binds
only non-secret live target and credential-generation evidence. It rebuilds
supported source preparation and governance, scans the current categorical
domains, and saves a normal `MappingWorkingDraft`. Impodo stages
manager-authored quality rules against that exact mapping hash.
`QualityService` regenerates automatic rules after mapping confirmation.

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

Registration validates the requirements for the selected source mode. If they
pass, Impodo moves the workspace to `REGISTERED`, increments its optimistic
revision, writes canonical registration evidence, and records an actor-bound
audit event. File mode requires at least one source file but does not require a
destination. Odoo source mode requires an exact connection identity and a
successful browser check. The workflow then enters the appropriate source
inspection or capture path. `build_project_navigation` derives the stage
status.

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
