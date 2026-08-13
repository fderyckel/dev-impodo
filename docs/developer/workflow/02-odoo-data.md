---
audience: developer
stage: odoo
status: current
---

# Odoo data

## Responsibility

Odoo data captures the selected Odoo 19 model and field metadata. In file mode,
it also governs the business keys used by mapping and comparison. It owns
metadata provenance and the confirmed target-schema boundary.

It does not read an unrestricted business-record export and does not expose a
generic RPC escape hatch.

## Entry conditions

The project target is configured. File mode also requires frozen source data.
Odoo source mode reaches this responsibility before source records are frozen
because eligible fields define what may be captured.

## Implementation flow

`schema.py` refreshes the permitted model catalogue, saves the selected scope,
captures field details, and, for file mode, submits key governance.
`SchemaWorkspaceService` coordinates the model catalogue, schema catalogue,
source selection, and mapping invalidation ports.

Local capture uses the isolated local reader. Remote capture uses the narrow
JSON-2 read connector. Both normalize metadata into the same domain catalogue
before governance is saved.

## Code references

| Role | Code |
| --- | --- |
| Schema orchestration | [`SchemaWorkspaceService`](../../../src/impodo/application/schema_workspace_service.py) |
| Schema governance | [`governance.py`](../../../src/impodo/domain/schema/governance.py) |
| Browser routes | [`schema.py`](../../../src/impodo/web/routers/schema.py) |
| Local reader | [`local_odoo_reader.py`](../../../src/impodo/local_odoo_reader.py) |

## Evidence and state

The model catalogue records the available scope. The schema catalogue binds
models, fields, types, requirements, selections, relations, and target
provenance. In file mode, `SchemaGovernance` binds the confirmed business-key
rules to that exact schema revision.

Stable technical model and field names are evidence; translated UI labels are
presentation. Numeric database IDs must not become portable identities.

## Completion and navigation

File mode completes only when both the schema catalogue and schema governance
exist, then unlocks Match data. Odoo source mode completes its first
responsibility when the eligible schema exists, then unlocks the bounded
capture and freeze responsibility.

## Invalidation and recovery

Recapture or governance changes invalidate dependent mapping revisions and
later artifacts. Local draft capture is a deliberate development path and may
not be presented as live Odoo evidence. Connector failures must retain the
upstream cause instead of being reduced to a generic browser status.

## Odoo 19 and performance

Read capability is explicit and narrow: model catalogue, metadata, target
fingerprint, and planned record requests. Batch metadata and record reads by
model. Never call `fields_get`, selection providers, or relationship catalogues
inside a source-row loop.

Odoo 19 inherited fields and dynamic selections must come from the connected
database. Do not hard-code a standard-only catalogue when custom modules are in
scope.

## Verification

- [`tests/test_workspace.py`](../../../tests/test_workspace.py)
- [`tests/test_local_odoo_reader.py`](../../../tests/test_local_odoo_reader.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)

Verify inherited fields, selection normalization, business-key revisioning,
read-only capability, batched requests, invalidation, and both source modes.

## Related documentation

- [User guide: Odoo data](../../user/workflow/02-odoo-data.md)
- [Browser workspace contract](../../contracts/02-workspace.md)
- [Architecture decisions](../../decisions/README.md)
