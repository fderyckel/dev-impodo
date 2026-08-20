---
audience: user
stage: load
status: current
---

# Load into Odoo

## Goal

Explicitly load the exact reviewed plan for the current Recipe data version
into its approved Odoo 19 target, then verify the recorded outcome.

## Before you start

The current final review must be **Ready**. Confirm the data-version purpose,
target, exact write totals, dependency order, writable fields, and required API
key. A Test data version is a rehearsal. A Production data version must come
from the exact qualified Recipe revision selected for rollout and still needs
fresh Production approval and credentials.

## Steps in Impodo

1. Open **Load into Odoo**, then review **Check changes**.
2. Confirm the target, exact snapshot, new and changed totals, field scope, and
   dependency order.
3. Continue to **Confirm and load**.
4. Enter the separate approved write API key when requested.
5. Read the explicit confirmation and select the single load action once.
6. Wait for the execution result; do not resubmit an uncertain request.
7. Open **Verify result** to read back the affected records.
8. Review reconciliation and download fallout details when any row cannot be
   verified.

![Current Check changes screen with exact new, changed, up-to-date, and per-table totals.](../../images/user/17-load-preview.png)

![Current Confirm and load screen with the separate write-key field and one explicit load action.](../../images/user/17b-load-confirmation.png)

## How Recipes use the verified outcome

Loading does not change or republish the Recipe. For a Test data version, a
successful load is not enough: complete **Verify result**, then return to the
Recipe overview to qualify the exact tested revision. Selecting that
qualification as the rollout candidate is another explicit action.

Production starts as a clean data version from that selected candidate. It
does not inherit Test files, server settings, credentials, comparison,
approval, execution, or read-back evidence. Production loading is authorized
only by the exact fresh Production review and its separate current write key.

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
provide whole-migration rollback, qualify a Recipe by itself, or carry Test
write authority into Production. Unchanged and blocked rows are not written.

## Needs attention

Do not blindly retry a timeout, connection reset, HTTP 422, or other unknown
write outcome. First inspect the execution journal and reconcile the target.
Retry only through the recorded recovery path after determining which rows, if
any, were applied.

## What makes this work stale

A new source, schema, mapping, Recipe application, parameter, control,
preparation, comparison, target fingerprint, credential generation, or
dependency order invalidates the execution preview. Return to the earliest
changed stage and regenerate the evidence for this data version.

## Next stage

For Test, complete read-back, then qualify and select the exact Recipe revision
from its overview. For Production, keep the execution journal, reconciliation
result, and approved review package with the rollout record. Resolve any
fallout before considering the data version complete.

## Related documentation

- [Developer implementation: Load into Odoo](../../developer/workflow/06-load-into-odoo.md)
