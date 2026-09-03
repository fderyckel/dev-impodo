---
audience: developer
kind: contract
status: current
---

# Quality and quarantine contract

## Scope

Quality evaluation runs after canonical staging and before read-only Odoo
comparison. It never edits registered sources or canonical rows. It publishes
an immutable overlay and admits only eligible rows to preflight.

This is not package certification, execution approval, or Odoo write
authorization.

## Rules and outcomes

Mandatory checks cover required and bounded scalar values, governed lookups,
relationship readiness, and post-transformation identity collisions. Guided
cross-field checks are bounded and versioned with the exact mapping and schema.

Outcomes are:

- `WARNING` retains eligibility but requires review.
- `BLOCK` stops the record because setup or policy is incomplete.
- `QUARANTINE` sets the affected business record aside.
- `EXCLUDE` omits the record only under an explicit governed rule.

Unknown values, ambiguity, parsing failures, and unsupported contexts never
become silent exclusions. Identity collisions set aside the complete collision
group. A relation to a set-aside incoming record propagates a safe outcome
without an Odoo call.

When an incoming record forms part of a dependent record's target identity,
Impodo treats that parent and its dependent records as one update group. If one
dependent record is set aside, Impodo also sets aside the identity parent and
the remaining dependent records. This produces `QUARANTINE` evidence rather
than a run-level `BLOCK`, so unrelated record groups can continue to review.

## Complete accounting

Every canonical row has exactly one quality result. Every physical source row
has one accounting entry with links to all canonical records it contributed to.
This permits mixed outcomes when one physical row creates several records.

Unrepresented source rows, missing mandatory rule families, stale fields,
incomplete evidence, and inconsistent hashes fail closed. Set-aside and
governed-excluded rows are removed before Odoo request planning while their
evidence remains visible and reconciled.

## Publication and invalidation

Rules, runs, row results, issues, accounting links, and quarantine entries are
immutable project evidence. Publication is atomic and idempotent. Failure keeps
the previous current run; success advances the pointer and retains history.

Each run binds canonical staging, ruleset, mapping, schema, evaluator version,
ownership, and retention context. Any changed binding invalidates the current
quality result. Evidence excludes raw field values and numeric Odoo record IDs.

## Access and performance boundary

Quality evaluation makes no Odoo request. Evidence writes are bounded and
batched. Eligible Odoo reads are planned by model and paged; there is no
connector or database query per source row. Measured row limits and workstation
results belong in testing and reports.

## Related documentation

- [Prepare data implementation](../workflow/04-prepare-data.md)
- [Final review implementation](../workflow/05-final-review.md)
- [Canonical staging contract](canonical-staging.md)
