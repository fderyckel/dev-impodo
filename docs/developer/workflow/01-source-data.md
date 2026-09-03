---
audience: developer
stage: source
status: current
---

# Source data

## Responsibility

Source data converts either registered files or a bounded Odoo selection into
immutable DataVersion evidence. The DataVersion package owns inspection
catalogues, confirmed configuration, selection, freezing, Odoo-source capture,
and snapshot references. A workspace owns only its selected dataset references
and current transformation evidence.

It does not apply the final mapping, publish canonical staging, or perform an
Odoo write.

## Entry conditions

The workspace is `REGISTERED`. File mode requires contained registered files.
Odoo mode requires a captured eligible schema before the bounded record
selection can be saved and frozen.

## Implementation flow

For file mode, `workspace_setup.py` registers the selected files and invokes
the initial source inspection in the same **Use these files and continue**
request. It then opens the source preview. `sources.py` retains the explicit
recheck route and invokes `SourceWorkspaceService` to save per-file
configuration and freeze the selected tables. The frozen source snapshot is
built from hash-checked CSV or XLSX content and materialized as tagged Parquet
evidence.

`MigrationWorkspaceStateRepository` records each uploaded file in the draft
DataVersion package immediately. `DataVersionOwnedSourceRepository` reads and
writes catalogues and confirmations through that package. The workspace-engine
tables retained for snapshot creation and invalidation are derived caches; a
page never treats them as source-package authority.

File validation and inspection run in spawned, resource-bounded processes.
Each process receives the application build contract that accepted the parent
request and verifies it before opening the source file. A changed editable
installation therefore requires an Impodo restart instead of combining two
builds in one intake operation.

After every file is confirmed, the Source data page uses the already loaded
catalogues and configurations to render the dataset-name fields and the final
save action. `POST /workspaces/{workspace_id}/datasets/freeze` retains the hard
evidence boundary. Before a selection exists,
`GET /workspaces/{workspace_id}/datasets` returns the data manager to the inline
form; after the save, that route presents the saved-table result and the next
action.

For Odoo mode, schema capture occurs first. The source routes then save one
bounded capture selection for each selected model. A model-keyed current
pointer lets the data manager switch between Product and Unit of Measure
without displaying or replacing the other model's fields. The route holds the
read credential in session state, counts every complete plan, runs one capture
job, and publishes all frozen datasets through `OdooSourceCaptureService`.

`OdooCapturePublicationService` creates one tagged Parquet snapshot and one
protected origin sidecar per model. `OdooProvenanceRepository` advances the
complete manifest set, source selection, and snapshot pointers in one DuckDB
transaction. `WorkspaceDataVersionSourceService` accepts the same complete set
as the Data version's source evidence. A failed capture or publication leaves
the previous complete set current.

`derived_entities.py` routes optional lookup extraction and parent/child split
rules through `DerivedEntityWorkspaceService`. These rules remain plans until
full preparation expands them over the frozen source.

## Contract invariants

File intake accepts only bounded CSV and XLSX content. Inspection records the
file format, structure, formulas and errors, type suggestions, and bounded
samples. It inventories formulas but never executes them. Digit strings with
leading zeros remain text. Blank or duplicate headers block confirmation,
while other warnings require explicit acknowledgement.

Freeze assigns stable dataset and column identities and publishes every chosen
table as an immutable tagged Parquet snapshot with source-row lineage. The
selection and all snapshot pointers advance in one transaction. Mapping preview
and preparation read the verified snapshot rather than reopening CSV or XLSX.

An integrated Recipe run reaches the same freeze boundary from its run-owned
**Fresh data** page. It uses the selected Recipe revisions to propose the
physical tables and dataset names, but it still calls the ordinary confirmation,
freeze, and DataVersion projection services. Ordinary Authoring retains its
detailed table review; there is no second validation or snapshot implementation.

Each Odoo capture selection is append-only and bound to the current target
identity. Saving one model's selection does not contact Odoo and does not
replace another model's current selection. The complete set must contain one
plan for every model in the current schema, with distinct dataset names and at
most ten datasets. Current policy permits at most 50 closed scalar fields and
10,000 rows per model. The reader fetches 10, 100, or 500-row keyset pages as
the saved plan specifies. It shares the start and end identity and schema
checks across the set, but opens one bounded value stream per model. The live
reader accepts only service-generated requests. It exposes no raw domain,
arbitrary context, generic method, or caller-selected field path.

Each identity check computes one small company-scope fingerprint from the
primary and available company IDs. Assessment performs one identity and schema
check for the complete set. Capture performs one pair before and after the
complete set. Record pages are neither rescanned nor hashed, and consistency
validation does not compute a digest unless the workflow needs an evidence or
form token.

## Code references

| Role | Code |
| --- | --- |
| File and selection orchestration | [`SourceWorkspaceService`](../../../src/impodo/application/source_workspace_service.py) |
| Isolated source workers | [`source_worker.py`](../../../src/impodo/application/data_version/source_worker.py) |
| Shared source-file browser commands | [`source_file_commands.py`](../../../src/impodo/web/source_file_commands.py) |
| Odoo source capture | [`OdooSourceCaptureService`](../../../src/impodo/application/odoo_source_capture_service.py) |
| Atomic Odoo capture-set publication | [`OdooCapturePublicationService`](../../../src/impodo/application/odoo_capture_publication_service.py) |
| Protected Odoo origin evidence | [`OdooProvenanceService`](../../../src/impodo/application/odoo_provenance_service.py) |
| Data-version source acceptance | [`WorkspaceDataVersionSourceService`](../../../src/impodo/application/workspace_data_version_source_service.py) |
| Odoo capture jobs | [`OdooCaptureJobManager`](../../../src/impodo/application/odoo_capture_job_service.py) |
| Related-dataset plans | [`DerivedEntityWorkspaceService`](../../../src/impodo/application/workspace/derived_entities.py) |
| Source routes | [`sources.py`](../../../src/impodo/web/routers/sources.py) |
| Related-dataset routes | [`derived_entities.py`](../../../src/impodo/web/routers/derived_entities.py) |

## Evidence and state

The draft DataVersion package stores file references, content hashes, bounded
catalogues, and chosen physical-table configuration. The frozen
`SourceSelection` binds stable
dataset IDs, physical schema, row counts, source evidence hashes, and Parquet
storage. Odoo capture adds one selection, provenance sidecar, and target
binding per dataset without using numeric Odoo IDs as portable business
values. The model-keyed selection pointers and dataset-keyed manifest pointers
identify the exact current set. The `SourceSelection` binds all datasets as
one atomic source version.

Related-dataset rules are versioned workspace-owned evidence and must retain
complete source lineage when materialized later.

## Completion and navigation

File mode completes when a source selection exists and then unlocks Odoo data.
Odoo mode deliberately reverses the first two responsibilities. **Select data
to download** captures eligible fields and model-specific plans, then
**Download and freeze** publishes the selection. It then unlocks the separately
bound cross-instance destination workflow. Stages 4 through 7 connect that
destination, match every selected model, derive generic relationship order,
and approve an exact aggregate transfer package. Stage 8A rechecks that
package through a fresh read-only destination call. The path currently stops
before Stage 8B writes.

The source capture and destination checks use two distinct credential roles.
The source-fetch key cannot satisfy destination matching. The one destination
transfer key supports Stage 5 and Stage 8A reads, but no current Odoo-to-Odoo
route invokes it through a write-capable adapter.

## Invalidation and recovery

Before file-table freeze, add/remove commands use workbench revision checks and
delete only the selected DataVersion file and its dependent draft metadata.
After freeze,
source mutation fails closed. A changed hash, selection, capture, or
related-dataset plan invalidates downstream evidence; regenerate rather than
editing stored artifacts.

Background Odoo capture exposes explicit cancel and status routes. Do not
interpret an interrupted job as a published snapshot.

## Odoo 19 and performance

Odoo source capture must remain bounded by an explicit selection and eligible
field policy. Page reads are batched; adding per-row metadata or relationship
lookups would create an N+1 regression. Preparation must consume the frozen
snapshot and make zero Odoo calls.

The current derived or materialized preparation path has a lower row limit
than exact direct mappings; keep that limit visible rather than silently
falling back to unbounded Python work.

## Verification

- [`tests/integration/duckdb/test_workspace.py`](../../../tests/integration/duckdb/test_workspace.py)
- [`tests/application/data_version/test_source_worker.py`](../../../tests/application/data_version/test_source_worker.py)
- [`tests/domain/data_version/test_source_snapshot.py`](../../../tests/domain/data_version/test_source_snapshot.py)
- [`tests/integration/odoo/test_source_capture.py`](../../../tests/integration/odoo/test_source_capture.py)
- [`tests/application/data_version/test_odoo_capture_publication.py`](../../../tests/application/data_version/test_odoo_capture_publication.py)
- [`tests/application/data_version/test_odoo_capture_jobs.py`](../../../tests/application/data_version/test_odoo_capture_jobs.py)
- [`tests/application/workspace/test_derived_entities.py`](../../../tests/application/workspace/test_derived_entities.py)
- [`tests/integration/web/test_source_workflow.py`](../../../tests/integration/web/test_source_workflow.py)

Cover file hashing, configuration, pre-freeze replacement, post-freeze refusal,
per-model Odoo plans, complete-set capture, atomic publication, capture bounds,
cancellation, lineage, and both navigation variants.

## Related documentation

- [User guide: Source data](../../user/workflow/01-source-data.md)
- [Project lifecycle contract](../contracts/project-lifecycle.md)
- [Workflow evidence lifecycle](../contracts/evidence-lifecycle.md)
- [Related-table authoring](../../user/guides/related-tables.md)
