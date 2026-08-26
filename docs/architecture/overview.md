---
audience: developer
kind: architecture
status: current
---

# Impodo architecture

## Purpose

Impodo is a local browser application that helps a data manager prepare,
compare, and load governed data into Odoo 19.

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
|   |-- one mutable target setup before capture
|   |-- one immutable TargetBinding and unioned requirement plan
|   `-- RecipeApplication 0..many
|       `-- one isolated MigrationWorkspace
|           |-- bounded DataVersion dataset references
|           `-- fresh mapping and current application evidence
`-- zero, one, or several Recipes
    `-- immutable RecipeRevision lineage
`-- one versioned CutoverPlan
    |-- exact Recipe revisions, dependencies, ownership, and controls
    `-- immutable Test qualifications and Project rollout selections
```

The Project, DataVersion, MigrationRun, MigrationWorkspace, and Recipe each
have a distinct UUID. Supplying an identifier from another namespace fails
closed.

Choosing **New project** at `/projects/new` creates the Project, Authoring
DataVersion 1, Authoring run 1, and one open MigrationWorkspace in one
restart-safe workflow. It does not create a Recipe. The Project overview at
`/projects/{project_id}` shows the bounded registry projection and opens the
authoring work through `/workspaces/{workspace_id}`.

The registry also owns the workspace's `DRAFT` or `READY` setup state and its
optimistic revision. The contained engine has no independent workspace
lifecycle. Its flat `WorkspaceState` API is a workbench projection keyed by
`workspace_id`, not another identity or aggregate root. The engine persists a
singleton `workspace_projection_cache` with no embedded Project, DataVersion,
run, or workspace identity.

Every authenticated `/workspaces/{workspace_id}` request resolves one verified
`WorkspaceAccessContext` from the registry before route code opens a contained
store or external boundary. Authorization uses the context's genuine parent
Project ID. The request reuses that immutable lineage, and Odoo capture and
load workers receive the same context instead of separate UUID strings.
Missing, wrong-kind, mismatched, and inaccessible workspace identities return
the same opaque result before workspace evidence, credentials, protected
artifacts, or Odoo adapters open.

## Source and workspace boundary

One DataVersion owns the complete draft and accepted source package: immutable file
references, inspection catalogues, parsing confirmations, logical datasets,
source-snapshot references, and protected Odoo capture references when the
source is Odoo. Source bytes, snapshots, and protected Odoo origin sidecars are
stored under the DataVersion identity.

A MigrationWorkspace stores only the dataset identities and snapshot hashes
it selects from that frozen package in `workspace.duckdb`. The mapping engine
reads those contracts through `WorkspaceMappingSourceProjection`; it does not
copy the DataVersion database, source bytes, or source rows. During Authoring,
the contained engine retains bounded invalidation caches for draft source
inspection decisions, while canonical catalogues and confirmations are read
from and written to the DataVersion package. Preparation workers receive the
exact Project, DataVersion, run, application, and workspace identities before
spawn and verify both isolated stores against `workspace_linkage` before
reading evidence.

One MigrationRun owns the mutable Odoo target choice used during setup. A
successful target capture replaces that draft choice with an immutable
`TargetBinding`; neither value belongs to an individual workspace. Workspace
pages receive an explicit `WorkspaceOwnerView` containing the Project,
MigrationWorkspace, DataVersion, MigrationRun, source package, and optional
run target setup.

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

Completing these stages does not require Recipe publication. A Project can plan
one integrated Test run over several exact Recipe revisions and create fresh,
isolated application drafts. The Project binds that run to an immutable
CutoverPlan revision, requires ordered execution and verified read-back in
every application, publishes exact integrated qualification, and records
rollout selection separately. Production applies that selected meaning to a
fresh DataVersion and different Odoo 19 target with independent credentials
and evidence.

## Integrated Test run boundary

One accepted Test DataVersion supplies the complete source package. The run
planner selects only each Recipe's logical datasets, validates the dependency
graph and conservative field-level write ownership, and provisions one
workspace per application. The run captures one filtered Odoo 19 schema and
one filtered supporting-reference bundle for the union of requirements.
`MigrationRunTargetSchema` and `MigrationRunReferenceBundle` carry the
`migration_run_id`; they retain the source workspace provenance without
pretending that the run UUID is a workspace UUID.

Run-aware adapters expose only an application's required slice to the existing
mapping services and reject per-application target recapture. Integrated
status and issues are registry projections, so the run page does not open one
workspace database or call Odoo per Recipe.

## Integrated qualification boundary

One CutoverPlan revision pins the exact Recipe revisions, dependencies,
field-level write ownership, unioned requirements, and Project shared
controls. A downstream application cannot begin an Odoo write until each
declared predecessor has current verified reconciliation.

Qualification reads each application workspace once and makes no Odoo call.
Full per-application and integrated payloads are encrypted under a
Project-scoped key; the registry retains bounded identity and hash
projections. Changing selected Recipe meaning or dependency order appends a
new unqualified plan revision. Selecting a qualified revision records only a
rollout candidate and grants no Production authority.

## Production rollout boundary

A selected candidate can start a setup-only Production run. The setup creates
a fresh complete DataVersion, run, and workspace but no target binding or
write authority. Activation rechecks current source coverage, plan meaning,
Odoo compatibility, controls, write ownership, and separate read/write
credential evidence before creating isolated application workspaces.

Production uses the same compiler, source projections, run-level target
capture, workspace stages, execution journal, and reconciliation service as
Integrated Test. It copies no Test rows, credentials, mappings, comparisons,
approvals, or outcomes. The current selection and credential authority are
checked again before writer construction.

## Persistence layout

```text
<root>/registry.duckdb
<root>/.recipes-protected/
<root>/.project-evidence-protected/
<root>/artifacts/dv/<data_version_id>/
<root>/artifacts/ws/<workspace_id>/
<root>/projects/<project_id>/data_versions/<data_version_id>/data-version.duckdb
<root>/projects/<project_id>/workspaces/<workspace_id>/workspace.duckdb
<root>/projects/<project_id>/workspaces/<workspace_id>/workspace-engine.duckdb
```

The registry stores bounded Project, DataVersion, run target and requirement,
Production activation, Recipe application, workspace, Recipe, issue, and
operation projections. The
DataVersion database stores the source package. The small workspace database
stores exact source references. `workspace-engine.duckdb` is the contained current
mapping engine state. Its schema uses workspace-owned field names and audit
facts; the historical filename is not a Project aggregate identity. Every
workspace-store or engine open verifies the complete Project, workspace,
DataVersion, run, and optional application linkage first.

Impodo recognizes the current registry, DataVersion, workspace-store, and
`impodo-workspace-engine-2026-08-workspace-owned` engine generations. When one
of those databases has a supported older version, Impodo applies its complete
forward-only migration path in one transaction, records the applied steps,
and validates the exact current shape before normal repository access. An
interruption rolls back that database; the next open can safely retry it.

A different generation, a version below the supported baseline, a newer
version, a missing migration step, or a malformed shape is rejected without
mutation. Retired mixed-owner and Recipe-first generations still require the
reviewed development reset. Runtime adoption, semantic backfill, identity
aliases, dual reads, and dual writes remain absent.

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
repository call per source row. One normal workspace request resolves one
Project-owned lineage row. A progress request for a verified preparation,
Odoo-capture, or load job reuses its immutable lineage packet and performs no
second registry read.

## Main implementation boundaries

| Responsibility | Current implementation |
| --- | --- |
| Project, DataVersion, run, and workspace roots | `domain/project/models.py`, `application/project/service.py`, `domain/data_version/models.py`, `application/data_version/service.py`, `domain/run/models.py`, `application/run/service.py`, `domain/workspace/models.py`, `application/workspace/service.py` |
| Exact registry and isolated stores | `adapters/duckdb/migration_foundation_database.py`, `migration_foundation_repository.py` |
| Forward-only storage upgrades | `adapters/duckdb/schema/forward_upgrades.py` plus one versioned registry in each store schema module |
| Project-native creation | `application/migration_project_authoring_service.py` |
| DataVersion source ownership | `data_version_sources.py`, `application/workspace_data_version_source_service.py` |
| Mapping read projection | `application/workspace_source_projection.py` |
| Owner-specific artifact storage | `artifacts.py` (`DataVersionSourceArtifactStore`, `WorkspaceArtifactStore`) |
| Optional Recipe publication | `domain/recipe/models.py`, `application/recipe/service.py`, `application/recipe_publication_service.py`, `adapters/duckdb/recipe_repository.py` |
| Integrated Test planning | `migration_run_planning.py`, `application/run/planning_service.py`, `adapters/duckdb/migration_run_planning_repository.py` |
| Fresh Recipe application | `application/recipe_application_service.py`, `adapters/duckdb/run_aware_schema_repository.py`, `adapters/duckdb/run_aware_advanced_coverage_repository.py` |
| Browser composition | `web/app.py`, `web/routers/migration_projects.py`, `web/routers/integrated_runs.py`, `web/routers/workspace_setup.py` |

## Related documentation

- [Project lifecycle contract](../developer/contracts/project-lifecycle.md)
- [Recipe publication contract](../developer/contracts/recipe-lifecycle.md)
- [Evidence lifecycle](../developer/contracts/evidence-lifecycle.md)
- [Python code map](python-code-map.md)
- [Integrated run lifecycle](../developer/contracts/integrated-run-lifecycle.md)
