---
audience: developer
kind: report
status: current
---

# Odoo compatibility Phase 3: final Odoo 20 reads

## Result

Phase 3 qualifies the read-only Impodo boundaries on final Odoo 20. Connection,
schema capture, bounded Odoo-source capture, comparison reads, and Recipe
authoring are enabled for final `20.0` builds. Odoo 20 writes, recovery, and
Production remain disabled.

The live matrix passed against two separate deployments on 2026-09-23:

| Deployment | Version | Evidence | Result |
| --- | --- | --- | --- |
| Pinned local Community | `20.0`, upstream commit `dd5defe77fe1dc3bb02eddaca6e90d7daf34ed3d` | Local shell and loopback JSON-2 metadata, identity, bounded record reads, and bounded source capture | Passed; PostgreSQL and Odoo stopped afterward. |
| Temporary remote Enterprise Runbot | `20.0+e` | HTTPS JSON-2 metadata, identity, reference reads, and bounded source capture | Passed; the temporary API key was revoked before the receipt was written. |

The [machine-readable Phase 3 evidence](odoo-compatibility-phase3.json) contains
the exact module versions, model field counts, policy hashes, operation gates,
and limitations. It contains no credential, API key, session cookie, numeric
Odoo identifier, or record value.

## Supported boundary

The following final Odoo 20 operations are enabled:

- Connect and identify the exact database and read principal.
- Discover models and capture the effective live schema.
- Capture a bounded Odoo source selection through the closed JSON-2 adapter.
- Read bounded target and governed reference values for comparison.
- Author and assess an Odoo 20 Recipe against Odoo 20 schema evidence.

The following operations remain blocked:

- Any Odoo 20 Test or other business-data write.
- Interrupted-write recovery on Odoo 20.
- Odoo 20 Integrated Test qualification and Production activation.
- Cross-major Recipe use or Odoo-to-Odoo transfer.

Odoo 19 retains its existing operation set and existing compatibility aliases.
Odoo 19 and Odoo 20 have distinct current source-capture and governed-reference
policy hashes. Historical Odoo 19 hashes remain readable; new Odoo 20 evidence
cannot silently use an Odoo 19 policy identity.

## Verified schema differences

The final Odoo 20 schema is not treated as Odoo 19 with a different version
label. Live evidence confirmed these changes in the qualified Manufacturing
and Product scope:

| Model | Odoo 19 field removed from this contract | Final Odoo 20 field |
| --- | --- | --- |
| Bill of Materials (`mrp.bom`) | `product_uom_id` | `uom_id` |
| Bill of Materials Line (`mrp.bom.line`) | `product_uom_id` | `uom_id` |
| Product (`product.template`) | `uom_po_id` | One shared `uom_id` remains. |
| Unit of Measure (`uom.uom`) | `category_id`, `rounding`, `uom_type` | `relative_factor`, `relative_uom_id`, stored `factor`, and `sequence` |

Impodo captures these native fields and does not alias one name to another. An
Odoo 19 mapping or Recipe that refers to a removed field therefore remains an
Odoo 19 artifact and is rejected for Odoo 20. Unit semantics need functional
review: a category and a reference unit are not interchangeable concepts.

All required contracts matched between the local-shell and JSON-2 readers. The
full `res.partner` contract had one non-blocking visibility-dependent difference
on `tz`; the required contact fields matched. This is retained as explicit
evidence rather than hidden behind a claim that both readers return identical
metadata for every access context.

## Implementation

The shared compatibility policy enables only the qualified read operation set
for final Odoo 20. Prerelease, SaaS, later-minor, malformed, and future-major
forms remain blocked. Existing Odoo 19 defaults remain compatibility aliases,
while live schema, source selection, supporting-reference capture, mapping
validation, Recipe compilation, preflight planning, and comparison select the
policy for the detected major explicitly.

The lab manifest pins the final upstream commit, Python 3.12, PostgreSQL 17,
separate ports, and a separate database. The preparation and qualification
runners reject a mismatched receipt. The qualification runners emit sanitized
receipts only after temporary access has been revoked and local services have
been stopped.

## Verification

The two live qualification commands were:

```powershell
.\.venv\Scripts\python.exe scripts\qualify_odoo20_reads.py
$env:IMPODO_RUNBOT_PASSWORD = '<temporary Runbot password>'
.\.venv\Scripts\python.exe scripts\qualify_odoo20_runbot_reads.py `
  --base-url https://126209162-20-0-all.runbot147.odoo.com `
  --database 126209162-20-0-all
```

Focused automated verification passed 187 tests: 111 version, policy,
reference, adapter, source-capture, and Recipe tests, plus 76 schema-governance,
publication, preflight, planning, and integrated-Recipe-run tests. The live
runners separately exercised nine models and a two-row bounded source capture
on both Community and Enterprise deployments.

This phase does not satisfy the write and release gates in Phases 4 and 5.
Before enabling Odoo 20 writes, Impodo still needs the final-build create,
update, rejected-write, lost-response, read-back, recovery, same-major
transfer, Integrated Test, Production, custom-field, permission, multi-company,
and performance matrix described in the support plan.
