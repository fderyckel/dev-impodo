---
audience: developer
kind: contract
status: current
---

# Execution and reconciliation contract

## Scope

Execution applies only the exact writes frozen by a ready final-review result.
It uses a narrow write capability, journals intent before transport, stops on
unknown outcomes, and proves the resulting target state through a separate
read-back capability.

It exposes no generic Odoo client and provides no whole-migration rollback.

## Authorization and binding

Execution requires a current `READY` report, execution snapshot, target
fingerprint, mapping, prepared evidence, writable-field scope, dependency order,
explicitly permitted target, verified actor, and write-role credential.

Before a write, the service revalidates every binding and records the
credential generation, write principal, observed permissions, and context as
non-secret hashes. Any mismatch fails closed.

The execution snapshot carries an opaque binding hash for each uniquely
reviewed existing row and target relationship. It never carries a numeric Odoo
ID. Immediately before journaling, execution bulk-resolves all existing keys
in bounded model pages. Every key must still be unique and must produce the
same opaque binding. A missing, ambiguous, or retargeted key sends the data
manager back to **Check changes** before the first Odoo write.

Completed-load correction is the deliberate protected exception. Its lean
correction execution snapshot is rebuilt from the encrypted plan and explicit
confirmation, so it carries the exact numeric IDs already proved by the
completed run. It exposes no lookup fields and performs no business-key search.
Immediately before journaling, it rereads those IDs in bounded pages and
requires every affected value to equal the confirmed current value. Any
difference invalidates the current plan and sends zero writes.

## Journal-before-transport

The durable run and planned row attempts are committed after the read-only
crosswalk check and before the first write. Immediately before each Odoo call,
the journal marks the exact rows `IN_FLIGHT` and stores their component, page,
transport-batch number, and create, update, or relationship-completion phase.
The returned outcome then replaces that checkpoint in a second short
transaction. A process exit between those transactions therefore leaves
durable evidence of the exact call whose response was not recorded.

Every result distinguishes planned, in flight, safe to retry after read-back,
committed, partially applied, failed, blocked, and outcome unknown. Transport
responses never overwrite earlier journal evidence.

The execution snapshot stores one deterministic row schedule derived from the
reviewed incoming business keys. Execution consumes each topological component
in bounded pages and never mixes rows from different components in one page.
It omits only the optional fields named by the snapshot's exact completion
list and writes those fields after their required create receipts exist. Every
retained create dependency passes an explicit receipt barrier: the dependency
must have a journalled committed or partially applied result and an exact
created identifier before the dependent write. A required row cycle or
unusable dependency stops before the journal and target transport. A deferred
row remains explicit if it is only partially applied.

A reviewed incoming relationship may name one captured, read-only many2one
field on its source model when Odoo creates the required related record from
that source. The snapshot freezes that projection field and target model.
After the direct create receipt is durable, execution reads the projection in
exact pages of at most 500 identifiers and journals the projected receipt
while the source row is `PARTIALLY_APPLIED`. A dependent write cannot consume
the projected identifier until that journal transition succeeds. The adapter
does not infer Product or BOM model names or expose a caller-selected Odoo
method.

## Unknown outcomes, interruption, and retry

A connection reset, timeout, or wrapped upstream error may leave target state
unknown. The service records the affected batch, stops all later writes, and
requires reconciliation. A caught unknown response remains
`OUTCOME_UNKNOWN`; the service does not retry it inside that execution.

An unexpected process exit can instead leave a `RUNNING` execution with one
`IN_FLIGHT` batch. `ReconciliationService.assess_recovery` reads that immutable
snapshot's exact model and field scopes without publishing a final
reconciliation result. `ExecutionService.resume` accepts only that hash-bound
report, the original target and write identity, and a still-current snapshot.
It atomically binds every row to the recovery-report hash before another Odoo
call can begin.

Recovery may mark an interrupted create `RETRY_READY` only when business-key
read-back found no matching record. It may accept a write as committed only
when read-back proves every intended final field. It may retain a created row
as `PARTIALLY_APPLIED` only when all non-deferred fields match and the differing
fields are contained by that row's frozen relationship-completion fields. A
created row whose required projected receipt is absent also remains partially
applied. Resume re-reads the frozen projection from the already created source
identifier; it never recreates the source or accepts a changed projected
identifier. A
completed earlier component that changed, an ambiguous match, a missing
receipt, another target, or another principal stops resume. Known rejections
and terminal `OUTCOME_UNKNOWN` runs require a new **Check changes** result.

## Reconciliation

Final reconciliation uses a separate read capability and only the affected
model, identity, and exact requested field scope. Rows for the same model but
different field sets are read in separate bounded groups. It proves final
scalar and relationship values, publishes new immutable evidence, and never
edits the execution journal.

Recovery assessment uses the same read-back logic but remains ephemeral.
Execution owns the distinct, atomic journal transition that records which
recovery report authorized a same-run resume.

A run is complete only when it has no unknown outcome and reconciliation proves
the expected target state. Otherwise navigation remains in a verify or
needs-attention state.

For scalar correction, reconciliation starts automatically after the journal
finishes. It rereads only exact protected IDs and affected fields. Verified
rows close the successor run and workspace; rejected, missing, different, or
still-unknown rows remain actionable reconciliation evidence.

## Browser projection boundary

Only a verified Authoring load with a published protected correction origin
may show **Correct completed load**. Publishing that origin closes the completed
workspace. Its ordinary authoring routes redirect to the focused correction
page, while a successor workspace remains the sole owner of corrected rules
and prepared evidence.

Correction review keeps at most one active review or apply job per completed
workspace and retains its safe progress projection across page reloads. If the
submitted mapping still matches the completed mapping, review stops before
Parquet preparation and before an Odoo read. Otherwise the existing native
Polars preparation path reduces previous and corrected Parquet snapshots to
sparse candidates before the exact-ID target read.

The correction page may expose only grouped field and record counts,
already-correct counts, bounded blocker explanations, and verified or
needs-attention outcomes. Numeric Odoo IDs, business values, source-row
numbers, semantic hashes, credentials, and encrypted artifact references stay
outside browser state. Apply requires an explicit confirmation for the current
record count; a refreshed plan or changed rule invalidates an older
confirmation.

The review UI derives its explanation from the current immutable execution
snapshot. It may show at most five ordered load groups, at most three prepared
record-type labels per group, and at most five grouped blocker categories.
Longer plans report omitted counts. The browser projection carries no source
values or row identifiers and cannot change the approved schedule.

Blocker categories preserve the snapshot's stable reason code internally but
present a business explanation, affected-record count, bounded record-type
labels, and one next action. Unknown codes fall back to a fail-closed review
message; they do not create an execution exception.

Progress is a projection of durable journal states. A `PLANNED`, `IN_FLIGHT`,
or `RETRY_READY` row is not final. A `PARTIALLY_APPLIED` row has an accepted
create receipt but is not final until its frozen relationship fields or
generated-record receipt finishes.
Relationship progress cannot begin while a first-pass row remains unfinished.
The current load-group number and relationship totals are non-secret browser
control state only. Support details remain bounded to counts, target-safe
identifiers, and semantic hashes.

## Access boundary

Remote writes use named, scoped Odoo 19 JSON-2 operations. Read-back uses a
separate interface so a read-only component cannot invoke a write method.
Bounded crosswalk and write batching behavior are owned by the implementation
page.

## Related documentation

- [Load into Odoo implementation](../workflow/06-load-into-odoo.md)
- [Security and infrastructure](../../architecture/security-and-infrastructure.md)
- [Remote Odoo acceptance runbook](../runbooks/remote-odoo-acceptance.md)
