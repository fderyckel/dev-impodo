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

## Actionable set-aside evidence

Each relationship-readiness finding distinguishes the condition that Impodo
observed. `INCOMING_RELATIONSHIP_MISSING` identifies a key with no incoming
parent. `INCOMING_RELATIONSHIP_AMBIGUOUS` identifies a key with more than one
incoming parent. `INCOMING_RELATIONSHIP_PARENT_SET_ASIDE` identifies a
dependent whose matched parent was already unsafe.
`INCOMING_IDENTITY_DEPENDENT_SET_ASIDE` identifies the reverse propagation
that keeps an incoming identity group together.

The finding names the linked dataset and affected field. An inherited finding
also names one deterministic related source row when that row is available.
Full and bounded evaluation must publish the same reason, disposition, and
field evidence. The browser may group findings on the current page, but it
must label that scope and continue to show every finding on each record.

User-facing messages must not expose internal sentinels or dataclass
representations. Quality evidence remains value-free: the source row and
friendly field label guide the data manager back to the governed source or
mapping without copying raw business values into the quality ledger.

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
