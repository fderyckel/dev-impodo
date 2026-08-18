---
audience: developer
kind: contract
status: current
---

# Preflight contract

## Scope

The preflight engine serves strict CLI profiles and submitted browser mappings.
Without changing Odoo, it determines which exact prepared records are creates,
updates, unchanged, ambiguous, or blocked.

Its output is review evidence. It is not a clean-package certification,
execution plan, approval, or write authorization.

## Prepared-record boundary

Every nonblank input row produces an immutable prepared record, including rows
with validation issues. It separates source trace, target model, ordered
business identity/scope, typed scalars, symbolic relationships, and structured
issues.

Portable values include strings, arbitrary-precision integers, `Decimal`,
booleans, dates, timezone-aware UTC datetimes, null, and business-key
references. Portable records never contain numeric Odoo IDs.

Parsing continues far enough to retain row traceability. Source, type,
identity, duplicate, or relationship errors block the record rather than
silently dropping it.

## Odoo read boundary

The preflight port exposes only target fingerprint, requested model metadata,
and requested records. The live Odoo 19 JSON-2 adapter allowlists `fields_get`
and `search_read`; it exposes no generic method, mutation, SQL, import, server
action, or caller-supplied context surface.

Requests contain exact models, fields, and service-generated bounded domains.
Metadata and record requirements are merged by model and paged deterministically.
No connector call may occur inside a source-row loop.

## Target evidence

Metadata and record snapshots share one target fingerprint and bind the current
target, Odoo version, capture time, relevant modules, requested coverage, and
source/profile or browser bindings. Credentials and transport authorization are
excluded.

Numeric Odoo IDs are allowed only in protected target-specific snapshots and
indexes so relationships can be reverse-resolved to portable business keys.
Classification requires complete, matching snapshots.

Odoo selection codes are authoritative and labels are display-only. A changed
selection contract or removed code blocks affected evidence; choices are
indexed once per mapped field, not fetched per row.

## Resolution and classification

Source duplicates remain explicit. Target identity plus scope forms the lookup
key; target duplicates remain visible. Incoming and existing-target
relationships resolve through preloaded business-key indexes.

Every import candidate receives exactly one result in this precedence:

| Priority | Condition | Result |
| ---: | --- | --- |
| 1 | Blocking source, metadata, identity, relation, or comparison issue | `BLOCKED` |
| 2 | Multiple complete scoped target matches | `AMBIGUOUS` |
| 3 | No match and create requirements satisfied | `CREATE` |
| 4 | One match with material differences | `UPDATE` |
| 5 | One match without material differences | `UNCHANGED` |

Comparison uses the same declared type, normalization, precision, and null
policy for source and target values. Relationships compare through business
references.

### Pinned Odoo-source comparison

An Odoo-source project has no identity-resolution or create branch. The
service opens the protected capture origins, reads only exact numeric IDs in
bounded model-level chunks, and compares each approved field across captured
baseline, prepared proposal, and current Odoo value. It reuses the captured
Tier-1 type semantics for all three values.

The protected result distinguishes `UNCHANGED`, `UPDATE`,
`RECORD_REMOVED_OR_INACCESSIBLE`, `CONCURRENT_FIELD_CHANGE`,
`BASELINE_NOT_CAPTURED`, and `TARGET_SCHEMA_CHANGED`. Any result other than
`UNCHANGED` or `UPDATE` is portable `BLOCKED` evidence and requires a complete
capture refresh. Unrelated `write_date` movement is retained as evidence but
does not authorize overwriting an approved field that changed concurrently.

## Portable result

The canonical manifest binds source/snapshot hashes, input definition, target
fingerprint, totals, decisions, differences, relationships, issues, coverage,
and a semantic hash. Each decision retains its dataset, source row, business
identity/scope, match count, classification, differences, and issues.

Serialization recursively rejects numeric record IDs, credentials, and
transport authorization. Workbooks are projections of the manifest and contain
no independent classification logic.

For Odoo-source projects, the portable manifest additionally excludes all
baseline, proposed, and current business values. Those values and exact IDs are
application-encrypted with project/run/capture binding. The persisted record
snapshot is redacted, and no execution snapshot is published in Phase 6.

## Integration boundary

The browser preflight consumes the exact frozen preparation evidence. Mapping
submission, preparation, classification, functional approval, execution, and
reconciliation remain separate states. Any changed source, schema, mapping,
prepared evidence, target fingerprint, or dependency order makes the result
stale.

## Related documentation

- [Final review implementation](../workflow/05-final-review.md)
- [Preflight CLI runbook](../cli/preflight.md)
- [Profile authoring](../cli/profile-authoring.md)
