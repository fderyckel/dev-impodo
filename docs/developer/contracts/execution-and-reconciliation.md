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

Before target I/O, the service revalidates every binding and records the
credential generation, write principal, observed permissions, and context as
non-secret hashes. Any mismatch fails closed.

## Journal-before-transport

The durable run and planned row attempts are committed before the first write.
Every result distinguishes planned, committed, partially applied, failed,
blocked, and outcome unknown. Transport responses never overwrite earlier
journal evidence.

Datasets execute in the reviewed dependency order. Deferrable relationships
run only after required records exist and remain explicit if partially applied.

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
Batching behavior and current N+1 risks are owned by the implementation page.

## Related documentation

- [Load into Odoo implementation](../workflow/06-load-into-odoo.md)
- [Security and infrastructure](../../architecture/security-and-infrastructure.md)
- [Remote Odoo acceptance runbook](../runbooks/remote-odoo-acceptance.md)
