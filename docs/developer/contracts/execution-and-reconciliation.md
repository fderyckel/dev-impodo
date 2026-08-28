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

## Journal-before-transport

The durable run and planned row attempts are committed after the read-only
crosswalk check and before the first write.
Every result distinguishes planned, committed, partially applied, failed,
blocked, and outcome unknown. Transport responses never overwrite earlier
journal evidence.

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

## Unknown outcomes and retry

A connection reset, timeout, or wrapped upstream error may leave target state
unknown. The service records the affected batch, stops all later writes, and
requires reconciliation. It must not classify an unknown outcome as safely
retryable.

No retry is permitted until read-back establishes the exact target state and a
fresh comparison proves the intended next action. A repeat preview with any
unexpected create is a hard stop against duplicates.

## Reconciliation

Reconciliation uses a separate read capability and only the affected model,
identity, and field scope. It publishes new immutable evidence and never edits
the execution journal.

A run is complete only when it has no unknown outcome and reconciliation proves
the expected target state. Otherwise navigation remains in a verify or
needs-attention state.

## Access boundary

Remote writes use named, scoped Odoo 19 JSON-2 operations. Read-back uses a
separate interface so a read-only component cannot invoke a write method.
Bounded crosswalk and write batching behavior are owned by the implementation
page.

## Related documentation

- [Load into Odoo implementation](../workflow/06-load-into-odoo.md)
- [Security and infrastructure](../../architecture/security-and-infrastructure.md)
- [Remote Odoo acceptance runbook](../runbooks/remote-odoo-acceptance.md)
