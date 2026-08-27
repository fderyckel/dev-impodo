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

Every nonblank input row produces an immutable prepared record, including a row
with validation issues. The record stores the source trace and target model
separately from its ordered business identity and scope. It also stores typed
scalar values, symbolic relationships, and structured issues.

Portable values include strings, arbitrary-precision integers, `Decimal`,
booleans, dates, timezone-aware UTC datetimes, null, and business-key
references. Portable records never contain numeric Odoo IDs.

Parsing continues far enough to retain row traceability. Source, type,
identity, duplicate, or relationship errors block the record rather than
silently dropping it.

## Odoo read boundary

The preflight port exposes only the target fingerprint, requested model
metadata, and requested records. The live Odoo 19 JSON-2 adapter allowlists
`fields_get` and `search_read`. It exposes no generic method, mutation, SQL,
import, server action, or caller-supplied context.

Requests contain exact models, fields, and service-generated bounded domains.
Metadata and record requirements are merged by model and paged deterministically.
No connector call may occur inside a source-row loop.

Requirement-plan contract version 2 binds the governed-reference policy hash
and an ordered `ReferenceReadRequirement` for every supporting relationship
read. Each requirement preserves the captured parent model and relationship,
related model, ordered business key and scope, and requested fields. The Odoo
reader re-authorizes that complete reason against current schema evidence; a
flattened metadata or record request cannot grant access by itself.

A remote reader performs one exact identity probe for captured models and, if
needed, one combined identity probe for all authorized supporting models. A
local reader receives only models named by the plan. Both paths group bounded
record requests by model and domain, so neither source-row count nor the number
of supporting relations introduces a per-row Odoo call.

## Target evidence

Metadata and record snapshots share one target fingerprint. They bind the
current target, Odoo version, capture time, relevant modules, requested
coverage, and the applicable CLI profile or browser evidence. They exclude
credentials and transport authorization.

Numeric Odoo IDs are allowed only in protected target-specific snapshots and
indexes so relationships can be reverse-resolved to portable business keys.
Classification requires complete, matching snapshots.

Odoo selection codes are authoritative and labels are display-only. A changed
selection contract or removed code blocks affected evidence; choices are
indexed once per mapped field, not fetched per row.

## Resolution and classification

Source duplicates remain explicit. The target identity and its scope together
form the lookup key, and target duplicates remain visible. Preloaded
business-key indexes resolve relationships to both incoming and existing
target records.

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

The canonical manifest binds the source and snapshot hashes to the input
definition, target fingerprint, totals, decisions, differences, relationships,
issues, coverage, and semantic hash. Each decision records its dataset, source
row, business identity and scope, match count, classification, differences,
and issues.

Serialization recursively rejects numeric record IDs, credentials, and
transport authorization. The manifest remains the decision source for every
workbook status, issue, and count. A file-source workbook may add the exact
prepared values from the frozen input bound to that manifest so the data
manager can review what Impodo will load. It may also add protected display
values and rule explanations from the exact frozen normalization evaluation.
The workbook binds both inputs by content hash and joins their rows through the
source trace ID.

The workbook may label a cell as changed or added only from those frozen
normalization effects. A manifest field issue takes precedence. A blank with
no manifest issue remains informational; the workbook cannot create a blocker
by inspecting an empty cell. The final prepared value stays visible, while a
changed or added value may keep its original display value and rule explanation
inside an Excel note. This projection contains no independent classification
logic and cannot change saved evidence.

The workbook action queue contains manifest issues only. An error produces a
**Must fix** action, while a warning produces a **Review** action. The queue
sorts every required fix before review-only items and may show the exact final
prepared value only when the bound file-source evidence contains that field.
Safe transformations, neutral blanks, and workbook appearance cannot create an
action item. Traceability uses the accepted logical dataset and source row;
synthetic compiled source paths must not be described as original upload names.

For Odoo-source projects, the portable manifest additionally excludes all
baseline, proposed, and current business values. Those values and exact IDs are
application-encrypted with project/run/capture binding. The persisted record
snapshot is redacted, and current same-database pinned-update policy publishes
no execution snapshot. The portable workbook also excludes those protected
values.

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
