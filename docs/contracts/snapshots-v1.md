# Snapshot contracts v1

Snapshots are immutable, environment-specific inputs. Metadata and record
snapshots share the same fingerprint:

```json
{
  "environment": "DEV",
  "database": "uc_dev",
  "odoo_version": "19.0",
  "snapshot_timestamp": "2026-07-28T12:00:00Z",
  "module_versions": {"uc_core": "2.0.0"}
}
```

Secrets and connection URLs are forbidden.

## Metadata snapshot

```json
{
  "contract_version": 1,
  "kind": "metadata",
  "profile": {"id": "golden_slice", "version": "2.0.0"},
  "fingerprint": {},
  "complete": true,
  "limitations": [],
  "models": {
    "product.template": {
      "description": "Products",
      "fields": {
        "uom_id": {
          "type": "many2one",
          "required": true,
          "readonly": false,
          "relation": "uom.uom",
          "relation_field": null,
          "selection": []
        }
      }
    }
  }
}
```

The file contains only models, fields, and fixed metadata attributes in the
profile-derived request. When relevant module names are configured
programmatically, missing module-version permission appears in `limitations`
and does not invalidate otherwise complete metadata.

## Record snapshot

```json
{
  "contract_version": 1,
  "kind": "records",
  "profile": {"id": "golden_slice", "version": "2.0.0"},
  "fingerprint": {},
  "source_hashes": {"products.csv": "sha256:…"},
  "complete": true,
  "models": {
    "product.template": {
      "requested_fields": ["active", "default_code", "name", "uom_id"],
      "records": [
        {
          "id": 100,
          "values": {
            "active": true,
            "default_code": "P-100",
            "name": "Product",
            "uom_id": [10, "Unit"]
          }
        }
      ]
    }
  }
}
```

This is the only serialized milestone artifact that permits numeric Odoo IDs.
It is environment-specific, not portable, and is not an approval manifest.

Many2one values may be an ID or Odoo `[id, display_name]` pair. Many2many
values are ID arrays. Display names are ignored. IDs are reverse-resolved
through reference catalogs before comparison.

## Binding and integrity

When preflight loads saved snapshots, it verifies:

- identical environment fingerprints;
- selected profile ID and version when the binding is present;
- record-snapshot source hashes against the current source package when the
  binding is present;
- `complete: true`.

The SHA-256 of each saved snapshot file is recorded in the portable manifest.
Changing file bytes changes the semantic result hash.

Snapshot output uses canonical JSON and atomic `.partial` replacement.

Version 0.2.0 writes `contract_version`, `kind`, profile binding, and source
binding, but the loader does not yet apply a complete JSON Schema or require
every envelope field. It also does not persist a requirements-plan hash or
the requested domain. Saved live evidence should therefore be created by this
CLI, retained unchanged, and not replaced with hand-edited JSON.

## Fixture format

For local development, a combined fixture contains:

```json
{
  "metadata": {},
  "records": {}
}
```

`SnapshotConnector` projects that fixture through the exact same metadata and
record field requests as the live connector. It does not evaluate an Odoo
domain against fixture records; a fixture author must pre-scope the catalog.
The committed golden fixture is `fixtures/golden/target_snapshot.json`.

## Completeness

An incomplete record snapshot cannot be classified. An incomplete metadata
snapshot becomes a global blocking metadata issue for import candidates.

Live pagination uses deterministic `id asc` ordering, rejects duplicate IDs
across pages, and continues until it receives a page shorter than the
configured page size. A result whose size is an exact multiple of the page
size is followed by one additional empty page.

## Data minimization

Only fields needed for:

- identity and scope;
- comparison;
- relation matching;
- business-key reverse resolution

are requested and serialized. Full-table field exports, attachments, chatter,
and unrelated fields are outside the contract.
