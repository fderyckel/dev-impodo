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
metadata, and requested records. The live qualified-version JSON-2 adapter allowlists
`fields_get` and `search_read`. It exposes no generic method, mutation, SQL,
import, server action, or caller-supplied context.

Requests contain exact models, fields, and service-generated bounded domains.
Metadata and record requirements are merged by model and paged deterministically.
No connector call may occur inside a source-row loop.

Requirement-plan contract version 4 binds the governed-reference policy hash,
dependency edges, and an ordered `ReferenceReadRequirement` for every
supporting relationship read. Each requirement preserves the captured parent
model and relationship, related model, ordered business key and scope, and
requested fields. The Odoo reader re-authorizes that complete reason against
current schema evidence; a flattened metadata or record request cannot grant
access by itself.

A nullable self-referencing identity scope produces one exact domain
expression for each prepared lineage. A root ends with an unset relationship,
while every descendant extends the relationship path through its parent. The
planner batches those expressions into bounded requests. It never falls back
to the leaf name or an unrestricted read.

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

The readiness report header also carries a value-free complete-run issue
aggregate. Publication groups every blocking decision issue and ambiguous
target match by dataset, stable technical code, field label, and action while
the complete decisions are already in memory. It stores only the affected
record count and portable labels; record identities, values, numeric Odoo IDs,
and target evidence remain in the existing decision rows and protected
artifacts. This aggregate is a read-only projection of manifest evidence, not
a second issue authority.

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

The local browser resolves authorization, Workspace lineage, current evidence
bindings, read credential, canonical comparison operation, and bounded semantic
input hash inside one local operation before it enqueues work. The existing
local registry operation-intent table owns value-free phases and the verified
result receipt; the Workspace engine continues to own the complete readiness
report and current pointer. The report header binds the operation ID, but the
portable manifest and execution snapshot hashes do not include control-plane
progress.

The reserved semantic hash and read-credential binding travel with the worker
request. The worker rejects changed meaning before reading Odoo and before
publication; the publication transaction also requires the prepared Workspace
revision. A changed queued or in-flight input must leave the earlier current
report intact.
The browser worker reads the current saved credential generation again inside
these local checks rather than reusing only its enqueue-time value.

The comparison worker then uses three explicit phases. It loads
and verifies the complete frozen input inside one local operation, releases
every retained database owner before the bounded Odoo read, and classifies and
publishes the returned snapshots inside a second local operation. Repository
transactions remain independent until the preflight repository opens the
atomic publication transaction. No local database owner may cross the remote
Odoo boundary. Startup recovery marks an unfinished intent successful only
after the matching current report and immutable artifacts pass their hashes;
otherwise it records an interrupted or stale terminal state and leaves the
previous current pointer unchanged.

Receipt verification uses the source-specific artifact contract. File sources
require the verified canonical manifest and matching complete execution
snapshot. Pinned Odoo sources require the `odoo-pinned-comparison-v1` manifest,
its report bindings, and the matching encrypted comparison artifact hash. They
require no execution snapshot and no protected-value decryption for recovery.

A registration-file refresh happens after the publication transaction commits.
Its failure is a distinct `PreflightPublicationCommittedError` outcome and must
never delete committed artifacts or undo the saved report. A transaction
failure still rolls back and removes only that attempt's uncommitted artifacts.

The same publication transaction stores a versioned compact review projection
beside the scalar execution projection. It contains bounded per-dataset counts,
at most five load-order groups, at most five blocker groups, omitted-group
counts, and the hashes and credential bindings needed to prove that it belongs
to the current execution snapshot. It contains no source row, business
identity, field value, numeric Odoo identifier, credential, authorization
object, or write payload. The **Check changes** route reads this projection
without opening the complete execution snapshot. Load confirmation, submission,
execution, recovery, and reconciliation still reload and validate the complete
snapshot. A historical current report without a compatible compact projection
fails closed and requires a new comparison.

Decision publication encodes canonical rows in bounded batches of 1,000 and
uses an ordered DuckDB `INSERT ... SELECT` over each batch within the existing
single report transaction. Publication verifies the final inserted row count
before the current pointer advances. Timing diagnostics accept only
allowlisted phase names, elapsed durations, and call counts; they cannot retain
Workspace identifiers, paths, SQL, credentials, or row values.

## Related documentation

- [Final review implementation](../workflow/05-final-review.md)
- [Preflight CLI runbook](../cli/preflight.md)
- [Profile authoring](../cli/profile-authoring.md)
