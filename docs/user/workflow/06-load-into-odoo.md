---
audience: user
stage: load
status: current
---

# Load into Odoo

## Goal

Explicitly load the exact reviewed create and update plan into an approved
disposable Odoo 19 target, then verify the recorded outcome.

## Before you start

The current final review must be **Ready**. Confirm the target, exact write
totals, dependency order, writable fields, and required API key. This is a
rehearsal capability, not a production cutover procedure.

## Steps in Impodo

1. Open **Load into Odoo**.
2. Review the target fingerprint, exact snapshot, create and update totals,
   field scope, and dependency order.
3. Enter the approved API key when requested.
4. Read the explicit confirmation and select **Load into Odoo** once.
5. Wait for the execution result; do not resubmit an uncertain request.
6. Select the verification action to read back the affected records.
7. Review reconciliation and download fallout details when any row cannot be
   verified.

![Current Odoo load preview with exact create, update, no-change, and per-table totals.](../../images/user/17-load-preview.png)

![Current explicit load confirmation with batch size, write-key field, and one load action.](../../images/user/17b-load-confirmation.png)

## What to check

- The target is the intended disposable Local or Remote Odoo 19 database.
- The preview hash and totals are the current reviewed values.
- Every writable field is within the approved scope.
- Parent records precede dependent records.
- The journal records every attempted row.
- Read-back verification accounts for the final outcome.

## What Complete means

Either the reviewed snapshot required no writes, or execution finished and
reconciliation verified the expected Odoo state. A successful HTTP response
alone is not completion evidence.

## What changes and what does not

This is the workflow stage that can create or update Odoo records. It does not
provide whole-migration rollback. Unchanged and blocked rows are not written.

## Needs attention

Do not blindly retry a timeout, connection reset, HTTP 422, or other unknown
write outcome. First inspect the execution journal and reconcile the target.
Retry only through the recorded recovery path after determining which rows, if
any, were applied.

## What makes this work stale

A new source, schema, mapping, preparation, comparison, target fingerprint, or
dependency order invalidates the execution preview. Return to the earliest
changed stage and regenerate the evidence.

## Next stage

Keep the execution journal, reconciliation result, and approved review package
with the rehearsal record. Resolve any fallout before considering the project
complete.

## Related documentation

- [Developer implementation: Load into Odoo](../../developer/workflow/06-load-into-odoo.md)
