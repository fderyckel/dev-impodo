---
audience: developer
stage: load
status: current
---

# Load into Odoo

## Responsibility

Load into Odoo validates the current execution scope, journals every planned
write, executes the reviewed dependency order through a write-only capability,
and reconciles affected records through a separate read capability.

It does not provide a generic Odoo client or whole-migration rollback.

## Entry conditions

The current final-review report must be `READY`; its execution snapshot,
preflight hash, mapping, target fingerprint, writable fields, and dependency
order must still match. The target must be explicitly allowed for the practical
rehearsal path and the actor must provide the required write credential.

## Implementation flow

`execution.py` renders the preview, accepts the hash-bound confirmation, builds
the scoped executor, invokes `ExecutionService.execute`, and exposes
reconciliation and fallout routes.

`ExecutionService` validates the project and API scope, starts a durable run,
records planned rows before transport, executes datasets in dependency order,
stops after an unknown outcome, and records row-level results.
`ReconciliationService` then reads back the affected scope and publishes a
separate reconciliation run.

## Code references

| Role | Code |
| --- | --- |
| Execution orchestration | [`ExecutionService`](../../../src/impodo/application/execution_service.py) |
| Execution snapshot | [`execution_snapshot.py`](../../../src/impodo/domain/execution_snapshot.py) |
| Journal states | [`execution.py`](../../../src/impodo/domain/execution.py) |
| Reconciliation | [`ReconciliationService`](../../../src/impodo/application/reconciliation_service.py) |
| Browser routes | [`execution.py`](../../../src/impodo/web/routers/execution.py) |

## Evidence and state

The execution snapshot is semantic-hash bound. `ExecutionRun` and
`ExecutionRowAttempt` distinguish planned, committed, partially applied,
failed, blocked, and outcome-unknown states. Reconciliation is new evidence; it
does not rewrite the execution journal.

## Completion and navigation

No-change previews complete without transport. A write run completes only when
the journal has no unknown outcome and reconciliation verifies the expected
target state. Navigation reports **Verify outcome** or **Needs attention** when
the write result is not yet proven.

## Invalidation and recovery

Fail closed when any snapshot or scope hash differs. On
`OdooWriteOutcomeUnknown`, journal the affected batch, stop later writes, and
require reconciliation before any retry. Do not convert a connection reset or
wrapped HTTP 422 into a safe-to-retry failure.

Deferred relationships are applied only after their dependencies exist. A
partial relationship outcome remains explicit and recoverable through the
journal.

## Odoo 19 and performance

Remote writes use the Odoo 19 JSON-2 boundary with named, scoped operations.
Creates are grouped by compatible field shape and sent in bounded batches.
Updates currently perform identity resolution and `update_row` per record;
this is an N+1-sensitive path and must be measured before production-scale
claims. Any batching change must preserve per-row journaling and unknown-outcome
semantics.

Read-back reconciliation must batch by model and requested field scope. Keep
write and read interfaces separate so a nominally read-only component cannot
invoke a write method.

## Verification

- [`tests/test_execution_service.py`](../../../tests/test_execution_service.py)
- [`tests/test_execution_web.py`](../../../tests/test_execution_web.py)
- [`tests/test_execution_repository.py`](../../../tests/test_execution_repository.py)
- [`tests/test_reconciliation_service.py`](../../../tests/test_reconciliation_service.py)

Verify scope enforcement, dependency order, create batching, update behavior,
journal-before-transport, unknown outcomes, deferred relationships,
reconciliation, and repeat-preview safety against an explicitly disposable
Odoo 19 target.

## Related documentation

- [User guide: Load into Odoo](../../user/workflow/06-load-into-odoo.md)
- [Security and infrastructure](../../architecture/security-and-infrastructure.md)
- [Acceptance and test strategy](../../testing/acceptance.md)
- [Remote Odoo 19 acceptance](../../operations/07-remote-odoo-acceptance.md)
