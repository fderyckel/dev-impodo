---
audience: developer
stage: source
status: current
---

# Source data

## Responsibility

Source data converts either registered files or a bounded Odoo selection into
immutable project source evidence. It owns inspection, configuration,
selection, freezing, Odoo-source capture, and optional related-dataset plans.

It does not apply the final mapping, publish canonical staging, or perform an
Odoo write.

## Entry conditions

The project is `REGISTERED`. File mode requires contained registered files.
Odoo mode requires a captured eligible schema before the bounded record
selection can be saved and frozen.

## Implementation flow

For file mode, `sources.py` invokes source inspection and
`SourceWorkspaceService` to save per-file configuration and freeze the selected
tables. The frozen source snapshot is built from hash-checked CSV or XLSX
content and materialized as tagged Parquet evidence.

For Odoo mode, schema capture occurs first. The source routes then save a
bounded capture selection, hold the read credential in session state, run the
capture job, and publish frozen Odoo records through
`OdooSourceCaptureService`.

`derived_entities.py` routes optional lookup extraction and parent/child split
rules through `DerivedEntityWorkspaceService`. These rules remain plans until
full preparation expands them over the frozen source.

## Code references

| Role | Code |
| --- | --- |
| File and selection orchestration | [`SourceWorkspaceService`](../../../src/impodo/application/source_workspace_service.py) |
| Odoo source capture | [`OdooSourceCaptureService`](../../../src/impodo/application/odoo_source_capture_service.py) |
| Related-dataset plans | [`DerivedEntityWorkspaceService`](../../../src/impodo/derived_entities.py) |
| Source routes | [`sources.py`](../../../src/impodo/web/routers/sources.py) |
| Related-dataset routes | [`derived_entities.py`](../../../src/impodo/web/routers/derived_entities.py) |

## Evidence and state

File intake stores content hashes and bounded catalogues. File configuration
records the chosen physical tables. The frozen `SourceSelection` binds stable
dataset IDs, physical schema, row counts, source evidence hashes, and Parquet
storage. Odoo capture adds selection, provenance, and target bindings without
using numeric Odoo IDs as portable business values.

Related-dataset rules are versioned project-local evidence and must retain
complete source lineage when materialized later.

## Completion and navigation

File mode completes when a source selection exists and then unlocks Odoo data.
Odoo mode deliberately reverses the first two responsibilities: **Odoo source
data** captures eligible fields, then **Freeze Odoo records** publishes the
selection. The current Odoo-source navigation keeps Match data and later stages
locked even after capture because preparation still requires the file-source
binding. Do not document the planned round-trip path as implemented.

## Invalidation and recovery

Before file-table freeze, add/remove commands use project revision checks and
delete only the selected contained file and its local catalogue. After freeze,
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

- [`tests/test_workspace.py`](../../../tests/test_workspace.py)
- [`tests/test_source_snapshot.py`](../../../tests/test_source_snapshot.py)
- [`tests/test_odoo_source_capture.py`](../../../tests/test_odoo_source_capture.py)
- [`tests/test_derived_entities.py`](../../../tests/test_derived_entities.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)

Cover file hashing, configuration, pre-freeze replacement, post-freeze refusal,
Odoo capture bounds, cancellation, lineage, and both navigation variants.

## Related documentation

- [User guide: Source data](../../user/workflow/01-source-data.md)
- [Migration project contract](../../contracts/01-migration-project.md)
- [Browser workspace contract](../../contracts/02-workspace.md)
- [Related-table authoring](../../operations/08-related-dataset-authoring.md)
- [Odoo source import and round-trip update plan](../../plans/odoo-source-import-plan.md)
