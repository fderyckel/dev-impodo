# Impodo architecture

## Purpose

This document is the current structural overview of Impodo. It explains the
main components, data flow, and boundaries without repeating the detailed
[developer contracts](../developer/README.md#normative-contracts) or
[architecture decisions](../decisions/README.md).

Impodo is currently a local browser application for governing source data and
running a bounded Odoo 19 migration workflow. The supported `FILE` path covers
CSV/XLSX intake, mapping, preparation, review, read-only comparison, an
explicit disposable local or remote load, journaling, and reconciliation. The
browser also implements bounded `ODOO` source capture and immutable snapshot
publication, but preparation does not yet accept an Odoo-origin source binding,
so that round-trip path is not end to end. Production loading, clean-package
certification, a separate functional mapping-approval lifecycle, and hosted
operation remain later capabilities described in the
[product vision](../product-vision.md).

## System context

```mermaid
flowchart LR
    User["Data manager"] --> Browser["Managed browser"]
    Browser --> Web["Impodo / FastAPI<br/>127.0.0.1 only"]
    Web --> Services["Project and workspace services"]
    Services --> Store["Per-project DuckDB"]
    Services --> Files["Governed project files"]
    Services --> Worker["Isolated inspection and<br/>preparation workers"]
    Services --> Capture["Bounded Odoo<br/>source capture"]
    Services --> Readers["Closed Odoo read<br/>and identity adapters"]
    Services --> Writer["Schema-bound<br/>Odoo writer"]
    Services --> Readback["Closed post-write<br/>read-back adapter"]
    Capture --> Odoo["Authorised Odoo 19 target"]
    Readers --> Odoo["Authorised Odoo 19 target"]
    Writer --> Odoo
    Readback --> Odoo
```

The browser is server-rendered. FastAPI coordinates application services; it
does not contain the core Recipe, project, mapping, or validation rules.
DuckDB and the filesystem are local adapters behind those rules.

## Current capability boundary

The browser implements:

- project registration with `FILE` or `ODOO` source mode;
- profile-free CSV/XLSX inspection, bounded previews, and immutable dataset
  freezing;
- bounded Odoo record selection, capture, protected provenance, and immutable
  Parquet snapshot publication;
- read-only Odoo model, field, identity, and permission capture;
- governed business keys, scalar mappings, transformations, and relations;
- bounded derived-entity authoring previews;
- immutable mapping revisions, semantic validation, and submission evidence;
- durable canonical staging, quality/quarantine evidence, normalization review,
  and prepared snapshots;
- read-only Odoo comparison with `CREATE`, `UPDATE`, `UNCHANGED`, `AMBIGUOUS`,
  and `BLOCKED` classifications;
- an exact execution snapshot, explicit disposable local or remote loading,
  a durable write journal, and post-write reconciliation.

The runtime has the Recipe-first authoring foundation. Every existing
project is represented by one registry-only Recipe and DataVersion shell;
opening that workspace hydrates an allowlisted Recipe setup projection and
writes exact local linkage. Published Recipe and qualification payloads use an
application-encrypted protected store, while bounded registry projections hold
lineage and recovery state. **New Recipe** provisions Recipe plus authoring
DataVersion 1. The Recipe overview projects publication readiness from exact
current workspace evidence, publishes portable immutable revisions, and shows
Recipe/DataVersion history. Current project URLs remain compatible through
explicit Recipe/DataVersion resolution. A published revision can now provision
a clean Test DataVersion, bind current replacement source and non-secret remote
Test target evidence, show focused drift, rebuild reusable preparation and
governance, and compile a fresh mapping draft. Matching retains its established
editor, with only surrounding Recipe-application status.

The repository also contains a profile-driven preflight engine and CLI. It is
a separate entry path that retains strict CSV and declared-sheet XLSX loading
while sharing compiled planning, comparison, and reporting semantics with the
browser path.

The supported `FILE` browser path is one executable bounded migration pipeline.
A submitted mapping alone is still only governed evidence: preparation,
normalization approval, a fresh comparison, an exact execution snapshot, and
one explicit **Load into Odoo** confirmation remain mandatory. Captured `ODOO`
sources currently stop before preparation because that service still requires
a file-source binding. Neither path is a production-cutover authorization.

## Browser workflow

**Project setup** records ownership, classification, retention, `FILE` or
`ODOO` source mode, and the selected `LOCAL` or `REMOTE` Odoo target. A
registered project then uses six browser stages:

1. **Source data** inspects and freezes CSV/XLSX datasets, or selects, captures,
   and publishes a bounded Odoo-source snapshot.
2. **Odoo data** discovers allowed record types and captures an effective,
   identity-bound Odoo schema through closed read and probe surfaces.
3. **Match data** records business keys, field providers, transformations,
   relationships, and derived-entity rules. Validation and submission bind the
   exact mapping revision to current evidence hashes.
4. **Prepare data** evaluates every supported frozen row, publishes canonical
   staging and prepared snapshots, and requires quality and normalization
   review. It accepts file-origin selections and supported immutable Odoo-origin
   captures; Odoo-origin preparation verifies the protected source provenance
   and remains offline after capture.
5. **Final review** reads Odoo in deterministic batches, classifies every row,
   and freezes the exact execution snapshot when the comparison is ready.
6. **Load into Odoo** requires an explicit confirmation, executes only the
   frozen schema-bound intentions, journals every attempt, and reads committed
   results back for reconciliation.

Changing source evidence, frozen datasets, target identity, schema, or
governed business keys invalidates downstream mapping evidence.

## Component layers

| Layer | Responsibilities | Main modules |
| --- | --- | --- |
| Browser | Local route composition, workflow routers, presenters, templates, sessions, CSRF, and security headers | `web/app.py`, `web/routers/`, `web/presenters/` |
| Application | Recipe lineage, authoring, Test application and recovery, project commands, intake, source capture/publication, schema governance, mapping, preparation, quality, normalization, preflight, execution, and reconciliation orchestration | `application/recipe_service.py`, `application/recipe_authoring_service.py`, `application/recipe_application_service.py`, `projects.py`, `intake.py`, `application/source_workspace_service.py`, `application/odoo_source_capture_service.py`, `application/odoo_capture_publication_service.py`, `application/schema_workspace_service.py`, `application/mapping_workspace_service.py`, `application/preparation_service.py`, `application/quality_service.py`, `application/normalization_service.py`, `application/preflight_service.py`, `application/execution_service.py`, `application/reconciliation_service.py` |
| Domain | Authorization, Recipe/DataVersion and application identities, exact TargetBindings, project lifecycle, source bindings and snapshots, mapping meaning, staging evaluation, execution snapshots, reconciliation, approvals, and deterministic values | `access.py`, `recipes.py`, `domain/recipe_applications.py`, `projects.py`, `domain/source_binding.py`, `domain/source_snapshot.py`, `domain/odoo_capture.py`, `domain/mapping/`, `domain/compiler/`, `domain/staging/`, `domain/execution.py`, `domain/reconciliation.py`, `approvals.py`, `models.py` |
| Local adapters | Focused DuckDB repositories, protected Recipe and Odoo payloads, artifacts, credentials, jobs, and resource-bounded workers | `adapters/duckdb/`, `adapters/protected_recipe_store.py`, `adapters/protected_odoo_provenance.py`, `artifacts.py`, `secrets.py`, `jobs.py`, `source_worker.py`, `application/preparation_job_service.py` |
| Odoo boundary | Remote JSON-2 identity and data reads, bounded source capture, fixed local metadata reads, schema-bound writes, post-write read-back, and local-stack readiness | `connectors.py`, `adapters/odoo_source_capture.py`, `local_odoo_reader.py`, `odoo_writer.py`, `odoo_readback.py`, `local_stack.py` |
| Preflight | Compiled semantics, frozen-row adaptation, bounded read planning, comparison, and reporting | `domain/compiler/`, `domain/preflight/`, `planner.py`, `metadata.py`, `catalog.py`, `engine.py`, `reporting.py` |

Domain and application modules do not depend on FastAPI templates. Adapters
may be replaced without changing lifecycle or mapping semantics.

## Persistence and evidence

On Windows, the normal local root is `%LOCALAPPDATA%\Impodo\projects`. A
configured macOS root uses owner-only permissions. The root contains a small
registry, an application-encrypted Recipe payload store, plus one directory
and DuckDB database per project. The registry owns Recipe and DataVersion
lineage, bounded application/qualification projections, cutover selection,
and restart-safe intents without scanning project databases. Project
directories separate inbox, staging, snapshots, reports, and audit artifacts;
canonical staging, prepared Parquet snapshots, protected target evidence,
execution journals, and reconciliation results are implemented within those
boundaries. The current build accepts one exact base project-database
generation and version, then applies only checksum-pinned additive Recipe
workspace migrations through a local ledger. A project from another base
generation or version is rejected rather than read through a compatibility
adapter.

Important evidence is immutable or versioned:

- source files retain their original bytes and SHA-256 hash;
- confirmed source selections and target schema captures are hash-bound;
- mapping revisions and submissions are immutable;
- canonical staging and prepared snapshots bind the compiled mapping and exact
  source selection;
- Odoo-source publications bind the capture plan, read identity, protected
  provenance, data hash, and current source snapshot;
- execution snapshots, write journals, and reconciliation runs remain separate
  hash-bound evidence;
- audit events retain stable actor identities;
- target-derived evidence names its connection-target, schema-scope,
  principal/context, and policy hashes explicitly;
- portable outputs use business keys rather than numeric Odoo IDs.

Numeric Odoo IDs are permitted only inside target-specific snapshots and
internal lookup indexes. They must not become portable source, mapping,
decision, manifest, or workbook identifiers.

## Odoo boundary

Remote reads use Odoo 19 JSON-2 through closed version, `context_get`,
`has_access`, `fields_get`, and `search_read` operations. Callers cannot select
an arbitrary model method or raw request context. Odoo-source capture adds only
policy-shaped, keyset-paginated `search_read` requests. Local metadata capture
uses a selected `odoo.conf` and fixed scripts for the model catalogue and
`fields_get`; it is not a generic Odoo shell. Local stack controls can stop
only services started and retained by the current Impodo session.

The read adapters have no create, write, unlink, import, arbitrary model method,
or SQL surface. The practical disposable-target path uses a separate writer
limited to exact lookups, remote External-ID `load` batches, bounded local
list-form creates, and single-record writes. Its model and field capability is
derived from one captured-schema-bound preview, so standard, extension, and
custom schema surfaces do not require a global product allowlist. Its frozen
snapshot, authorization, and journal are independent of the readers. A second
closed adapter performs exact-ID and governed-key `search_read` after the
write; the hash-bound result and concise fallout are persisted separately.

## Performance invariants

Odoo access must remain batched:

- request fields and records per model, not per source row or field;
- paginate target reads deterministically;
- build and reuse business-key and relation indexes;
- cache dependency resolution rather than rescanning datasets;
- split very large key domains into deterministic bounded requests.

No Odoo reader should call `fields_get`, `search_read`, `browse`, or another
ORM/RPC method inside a row loop. Odoo-source reads use fixed-size deterministic
pages. Remote creates use bounded External-ID `load` batches; local creates use
bounded list-form batches. The practical writer updates one uniquely re-matched
record per call because Odoo write failures must remain attributable to one
proposed row.

## Deployment boundary

The current composition root is local and single-user: loopback FastAPI,
local DuckDB, local artifacts, and a local credential vault. Inspection and
preparation use spawned worker processes; preparation progress is supervised by
the browser process. Odoo capture uses one bounded background thread. These job
control records are session-local rather than durable distributed-worker state.

A future hosted deployment uses a separate composition root with corporate
identity, project-scoped authorization, PostgreSQL, shared artifact storage,
durable workers, managed secrets, and a trusted TLS reverse proxy. Local
loopback assumptions must not be relaxed and reused as hosted controls. See
[ADR-008](../decisions/README.md).

## Authoritative detail

- [Security and infrastructure](security-and-infrastructure.md)
- [Project lifecycle contract](../developer/contracts/project-lifecycle.md)
- [Workflow evidence lifecycle](../developer/contracts/evidence-lifecycle.md)
- [Canonical staging contract](../developer/contracts/canonical-staging.md)
- [Preflight contract](../developer/contracts/preflight.md)
- [Normalization governance contract](../developer/contracts/normalization.md)
- [Quality and quarantine contract](../developer/contracts/quality-and-quarantine.md)
- [Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md)
- [Acceptance and test strategy](../testing/acceptance.md)
