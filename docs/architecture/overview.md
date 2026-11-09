---
audience: developer
kind: architecture
status: current
---

# Impodo architecture

Impodo's local browser application prepares source data, compares proposed
changes with Odoo, and loads approved changes into Odoo 19 or final Odoo 20.
The Linux server runs the shared control plane for accounts and administration.
The server entry point for the migration workflow refuses startup. See
[Security and infrastructure](security-and-infrastructure.md) and
[Server fact ownership](server-fact-ownership.md).

## Project, data, and workspaces

A Project holds one migration effort. Impodo creates a Project, a draft
Authoring DataVersion, an Authoring run, and an open workspace when the
operator submits **New project** at `/projects/new`. Retrying the same
operation returns the same identities. The Project overview at
`/projects/{project_id}` links to `/workspaces/{workspace_id}`.

| Object | What it owns |
| --- | --- |
| `MigrationProject` | Membership of its DataVersions, runs, workspaces, and optional Recipes. It also owns the CutoverPlan and rollout selection. |
| `DataVersion` | One source delivery. It holds file or Odoo capture references, inspection results, accepted source choices, logical datasets, and snapshot references. Accepting the DataVersion freezes this package. |
| `MigrationRun` | One use of a DataVersion. The run owns the draft target setup and, after capture, the immutable `TargetBinding`. Integrated runs also own shared target schema and reference evidence. |
| `MigrationWorkspace` | Work on selected datasets for one run. It holds source references and the current mapping, preparation, review, and load evidence. Each Recipe application has its own workspace. |
| `Recipe` | Immutable revisions of reusable rules. Each revision defines logical source inputs, mapping and preparation rules, Odoo requirements, business keys, checks, parameters, and controls. |
| `CutoverPlan` | Immutable revisions that pin the Recipes to run, their order, field-level write ownership, combined Odoo requirements, and shared controls. Test qualifications bind to exact plan revisions. |

For example, a Project may accept a workbook with products and bills of
materials. The DataVersion keeps both datasets. An application workspace for a
product Recipe reads the product dataset through its saved reference. A later
run can apply the same Recipe revision to a newly accepted workbook and
produce its own review and load evidence.

The registry owns each workspace's setup state and optimistic revision.
`WorkspaceState` is the mapping engine's view of the workspace. Its
`workspace_projection_cache` stores workbench state without duplicating
registry identity. See the [Project lifecycle contract](../developer/contracts/project-lifecycle.md).

## Authoring workflow

1. **Source data** inspects CSV and XLSX files or captures a bounded set of
   Odoo source records. The operator accepts the delivery as a DataVersion.
2. **Odoo data** uses read-only access to capture the permitted model and field
   definitions and record the business keys used for matching.
3. **Match data** records field mappings, transformations, relationships, and
   rules for which rows to include.
4. **Prepare data** builds canonical staging, checks data quality, and records
   normalization decisions.
5. **Final review** compares prepared rows with Odoo and freezes the reviewed
   operations for approval.
6. **Load into Odoo** checks authorization, journals writes, and reads records
   back to reconcile the result.

The Odoo-source transfer path captures source records separately and reviews
how to match and load them into another database of the same Odoo major
version. Eligible verified Authoring loads also support reviewed corrections
to previously loaded records. See [Load into Odoo](../user/workflow/06-load-into-odoo.md).

The operator can finish a one-off migration in the Authoring workspace.
To reuse its rules, the operator can save a Recipe from the Project overview
once the workspace has an accepted source delivery and current mapping,
schema governance, and quality evidence. Publication creates a Recipe and
its first revision, or adds an immutable revision to an existing Recipe.
The revision stores portable rules. Publication records its origin
separately, and each run keeps its own source rows, target access, and
execution evidence. See the [Recipe publication contract](../developer/contracts/recipe-lifecycle.md).

## Integrated Test and Production

A Test setup pins exact Recipe revisions and their dependency order. The
operator accepts a fresh DataVersion and checks an Odoo target of the major
version required by the Recipes. The run captures schema and supporting
references for their combined requirements. Activation creates one workspace
per Recipe application and binds the run to an exact CutoverPlan revision.
Each application reads only its required slice of the shared target evidence.

Before a dependent application writes, Impodo checks that each declared
predecessor has current, verified reconciliation. Qualification checks the
completed application evidence, execution order, and shared controls.
The registry stores identifiers and hashes, and Impodo encrypts the full
qualification evidence under a Project key. Changing the plan creates a new
revision that needs its own qualification before the operator can select it
for rollout. See the [Integrated Test run](../developer/contracts/integrated-run-lifecycle.md)
and [Cutover plan](../developer/contracts/cutover-plan-lifecycle.md) contracts.

Production setup uses the selected, qualified revision with a new file-based
DataVersion and a different target of the same Odoo major version. Activation
checks the source bindings, plan, controls, target, and separate read and
write credentials before creating application workspaces. Production uses the
same compiler and execution services as Test, with its own comparison,
approval, writes, and reconciliation. Before constructing a Production
writer, Impodo checks the current selection and credential authority again.
See the [Production run lifecycle contract](../developer/contracts/production-run-lifecycle.md).

## Local storage and access

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

The registry keeps compact Project and run records for list and status
pages. The DataVersion database stores source metadata and evidence
references. `workspace.duckdb` stores the selected dataset IDs and snapshot
hashes, and `workspace-engine.duckdb` stores the workspace's working
evidence. `WorkspaceMappingSourceProjection` reads source contracts and
snapshots through the DataVersion references.

Each authenticated workspace request resolves a `WorkspaceAccessContext` in
one registry query. The context identifies the parent Project, DataVersion,
run, workspace, and any Recipe application. Impodo checks access against that
Project before opening child stores, artifacts, credentials, or Odoo
adapters. It gives missing, mismatched, and inaccessible workspace IDs the
same response. Background workers reuse verified lineage. Preparation
workers check the linkage and build contract before reading either store,
and progress requests reuse the verified job context.

Each database has its own schema generation and version. The workspace
engine is at version 3. The registry, DataVersion, and workspace reference
stores are at version 1. Impodo validates linkage and schema before repository
access. It applies supported upgrades in one transaction per database and
rejects incompatible generations, versions, or malformed schemas without
changing the database. See
[Storage and release versioning](../developer/reference/storage-and-release-versioning.md).

## Odoo access and performance

Remote read adapters expose a fixed set of read-only JSON-2 operations.
Writes use a separate adapter and require current target evidence and explicit
approval. The compatibility policy checks the verified Odoo
version for the requested operation. See
[Odoo compatibility](../../src/impodo/domain/odoo/compatibility.py) and the
[Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md).

Project and run lists read registry summaries. Recipe publication readiness
can open the Authoring workspace, and an explicit qualification review can
read each application once without contacting Odoo. Odoo readers batch fields
and records by model. Comparison reuses lookup indexes. These paths avoid
repository or Odoo calls per source row.

## Code organization

`domain` defines business rules and contracts. `application` coordinates use
cases through ports. `adapters` implements storage, jobs, and Odoo access.
`web` handles HTTP and assembles the runtime. Mapping, preparation, and
execution are capabilities of a workspace or run, with evidence owned by
the corresponding workspace or run.

[Code organization](code-organization.md) explains dependency and database
rules. The [Python code map](python-code-map.md) traces the workflows to
implementation and tests.
