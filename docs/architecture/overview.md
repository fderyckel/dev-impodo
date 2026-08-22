---
audience: developer
kind: architecture
status: current
---

# Impodo architecture

## Purpose

Impodo is a local browser application that helps a data manager prepare,
compare, and load governed data into Odoo 19. The Project-first lifecycle in
[ADR-014](../decisions/README.md#adr-014--migration-projects-coordinate-reusable-recipes-and-cutover-plans)
is the current browser and persistence architecture.

A **Project** is the business and governance root. It can finish as one-off
migration work with no Recipe, or it can contain separately versioned Recipes.
A **Recipe** is an optional saved set of reusable transformation rules. A
Recipe never owns the Project's source data, DataVersions, runs, workspaces,
target credentials, approvals, or cutover decision.

## Ownership model

```text
MigrationProject
|-- DataVersion 1..many
|   `-- complete immutable source package
|-- MigrationRun 1..many
|   |-- one TargetBinding and unioned requirement plan
|   `-- RecipeApplication 0..many
|       `-- one isolated MigrationWorkspace
|           |-- bounded DataVersion dataset references
|           `-- fresh mapping and current application evidence
`-- zero, one, or several Recipes
    `-- immutable RecipeRevision lineage
```

The Project, DataVersion, MigrationRun, MigrationWorkspace, and Recipe each
have a distinct UUID. Supplying an identifier from another namespace fails
closed.

Choosing **New project** at `/projects/new` creates the Project, Authoring
DataVersion 1, Authoring run 1, and one open MigrationWorkspace in one
restart-safe workflow. It does not create a Recipe. The Project overview at
`/projects/{project_id}` shows the bounded registry projection and opens the
authoring work through `/workspaces/{workspace_id}`.

## Source and workspace boundary

One DataVersion owns the complete accepted source package: immutable file
references, inspection catalogues, parsing confirmations, logical datasets,
source-snapshot references, and protected Odoo capture references when the
source is Odoo. Source bytes, snapshots, and protected Odoo origin sidecars are
stored under the DataVersion identity.

A MigrationWorkspace stores only the dataset identities and snapshot hashes
it selects from that frozen package. The mapping engine reads those contracts
through `WorkspaceMappingSourceProjection`; it does not copy the DataVersion
database, source bytes, or source rows. Preparation workers receive the exact
Project, DataVersion, run, and workspace identities before spawn and verify
the isolated stores without opening the shared registry.

## Optional Recipe publication

After the data manager freezes source data and submits eligible mapping,
governance, and quality evidence, the Project overview offers **Save as a new
Recipe**. The compiler reads only the selected immutable dataset contracts and
current workspace evidence. Publication creates a Project-scoped Recipe and
Recipe revision 1 together through a restart-safe operation.

A later eligible workspace state can publish a successor revision under the
same Recipe identity. Publication records the origin Project, DataVersion,
workspace, mapping, schema, quality, actor, and time as provenance. It does not
change the Project ID, DataVersion ID, DataVersion owner, run, workspace, or
cutover authority.

A Recipe revision contains portable logical source shapes, preparation and
mapping rules, Odoo requirements, governed keys, reusable checks, references,
parameters, and controls. It excludes source rows, physical source identities,
target identity, credentials, numeric Odoo record IDs, approvals, writes,
read-back, and reconciliation evidence.

## Browser workflow

The authoring workspace retains the six data-manager stages:

1. **Source data** accepts and freezes uploaded CSV or XLSX data, or captures a
   bounded Odoo-source snapshot.
2. **Odoo data** captures the permitted Odoo 19 model and field contract with
   a read-only identity.
3. **Match data** records governed keys, mappings, transformations,
   relationships, and derived-entity rules.
4. **Prepare data** builds canonical staging, evaluates quality rules, and
   records normalization decisions.
5. **Final review** compares prepared rows with Odoo and freezes an exact
   execution snapshot when permitted.
6. **Load into Odoo** requires explicit confirmation, records each write, and
   reconciles the committed result.

Completing these stages does not require Recipe publication. M4 can plan one
integrated Test run over several exact Recipe revisions and create fresh,
isolated application drafts. Execution, integrated CutoverPlan qualification,
rollout selection, and Production application remain later phases. The
Project-owned flow does not restore the superseded Recipe-owned lifecycle.

## Integrated Test run boundary

One accepted Test DataVersion supplies the complete source package. The run
planner selects only each Recipe's logical datasets, validates the dependency
graph and conservative field-level write ownership, and provisions one
workspace per application. The run captures one filtered Odoo 19 schema and
one filtered supporting-reference bundle for the union of requirements.

Run-aware adapters expose only an application's required slice to the existing
mapping services and reject per-application target recapture. Integrated
status and issues are registry projections, so the run page does not open one
workspace database or call Odoo per Recipe.

## Persistence layout

```text
<root>/registry.duckdb
<root>/.recipes-protected/
<root>/artifacts/<data_version_id>/
<root>/artifacts/<workspace_id>/
<root>/projects/<project_id>/data_versions/<data_version_id>/data-version.duckdb
<root>/projects/<project_id>/workspaces/<workspace_id>/workspace.duckdb
<root>/projects/<project_id>/workspaces/<workspace_id>/project.duckdb
```

The registry stores bounded Project, DataVersion, run target and requirement,
Recipe application, workspace, Recipe, issue, and operation projections. The
DataVersion database stores the source package. The small workspace database
stores exact source references. `project.duckdb` is the contained current
mapping engine state; its historical filename is not a Project aggregate
identity.

Impodo opens only the exact M4 registry generation and unchanged exact M2
DataVersion/workspace-store generations. Earlier development or Recipe-first
storage is rejected without mutation and requires the reviewed development
reset. There is no upgrade, adoption, backfill, alias, or dual-write path.

## Odoo and performance boundaries

Remote access uses closed Odoo 19 JSON-2 operations. Read adapters cannot
create, write, unlink, import, execute SQL, or call arbitrary model methods.
The separate writer acts only after exact schema-bound evidence and explicit
confirmation.

Project and run list projections are bounded registry reads. The overview
currently opens only its one Authoring workspace when computing Recipe
publication readiness; it does not open a workspace per Project, DataVersion,
run, Recipe, or list row. Odoo readers batch fields and records per model,
comparison builds reusable indexes, and no workflow may introduce one Odoo or
repository call per source row.

## Main implementation boundaries

| Responsibility | Current implementation |
| --- | --- |
| Project, DataVersion, run, and workspace roots | `migration_projects.py`, `data_versions.py`, `migration_runs.py`, `migration_workspaces.py` |
| Exact registry and isolated stores | `adapters/duckdb/migration_foundation_database.py`, `migration_foundation_repository.py` |
| Project-native creation | `application/migration_project_authoring_service.py` |
| DataVersion source ownership | `data_version_sources.py`, `application/workspace_data_version_source_service.py` |
| Mapping read projection | `application/workspace_source_projection.py` |
| Optional Recipe publication | `project_recipes.py`, `application/project_recipe_publication_service.py`, `adapters/duckdb/project_recipe_repository.py` |
| Integrated Test planning | `migration_run_planning.py`, `application/migration_run_planning_service.py`, `adapters/duckdb/migration_run_planning_repository.py` |
| Fresh Recipe application | `application/project_recipe_application_compiler.py`, `adapters/duckdb/run_aware_schema_repository.py`, `adapters/duckdb/run_aware_advanced_coverage_repository.py` |
| Browser composition | `web/app.py`, `web/routers/migration_projects.py`, `web/routers/workspace_setup.py` |

## Related documentation

- [Project lifecycle contract](../developer/contracts/project-lifecycle.md)
- [Recipe publication contract](../developer/contracts/recipe-lifecycle.md)
- [Evidence lifecycle](../developer/contracts/evidence-lifecycle.md)
- [Python code map](python-code-map.md)
- [Integrated run lifecycle](../developer/contracts/integrated-run-lifecycle.md)
- [M4 implementation record](../plans/migration-projects-phase-m4-multi-recipe-runs.md)
