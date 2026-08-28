---
audience: user
stage: load
status: current
---

# Load into Odoo

## Goal

Explicitly load the exact reviewed plan for the current data version
into its approved Odoo 19 target, then verify the recorded outcome.

## Before you start

The current final review must be **Ready**. Confirm the data version purpose,
target, exact write totals, dependency order, writable fields, and required API
key. This key authorizes only the reviewed target operation; it does not grant
authority to another data project, data version, or future rollout.

## Steps in Impodo

1. Open **Load into Odoo**, then review **Check changes**.
2. Confirm the target, exact snapshot, new and changed totals, field scope, and
   dependency order.
3. Continue to **Confirm and load**.
4. If Stage 2 did not save a loading key, enter an API key approved for this
   load. If a loading key is available, Impodo uses it without asking you to
   enter it again.
5. Read the explicit confirmation and select the single load action once.
6. Wait for the execution result; do not resubmit an uncertain request.
7. Open **Verify result** to read back the affected records.
8. Review reconciliation and download fallout details when any row cannot be
   verified.

![Current Check changes screen with exact new, changed, up-to-date, and per-table totals.](../../images/user/17-load-preview.png)

![Current Confirm and load screen with the optional loading-key field and one explicit load action.](../../images/user/17b-load-confirmation.png)

## How saving a Recipe relates to the verified outcome

Loading does not create, change, or save a Recipe. If you save the
workspace's reusable rules, the resulting Recipe version still does not own
this execution or its read-back evidence.

Applying saved rules to replacement rollout data requires a fresh run. That
run must start with a new data version and
must not inherit this run's files, server settings, credentials, comparison,
approval, execution, or read-back evidence.

## What to check

- The target is the intended disposable Local or Remote Odoo 19 database.
- The preview hash and totals are the current reviewed values.
- Every writable field is within the approved scope.
- Parent records precede dependent records.
- The journal records every attempted row.
- Read-back verification accounts for the final outcome.

## How Impodo handles related records

Impodo reads the relationships that you confirmed in **Match data** and places
supporting record types before the records that use them. For example, it
loads reviewed units and categories before new Products, and reviewed Products
and bill of materials (BOM) headers before their component lines. You do not
need to arrange the source files in that order.

If two new records have an optional relationship to each other, Impodo creates
what it safely can and then finishes that relationship after both records
exist. If a required supporting record is missing, ambiguous, or cannot exist
first, **Check changes** must stop the load. Resolve that warning before you
select **Confirm and load**.

Impodo also freezes dependencies between rows in the same dataset. A parent
row therefore loads before its child when their reviewed business keys make
that order clear. Impodo uses the second relationship step only for an actual
optional cycle; it does not require you to rearrange an acyclic hierarchy.

Immediately before loading, Impodo checks existing Odoo records and related
records in bounded groups. A reviewed key must still point to the same unique
record. If it was removed, became ambiguous, or now points somewhere else,
Impodo creates no load journal and writes nothing; return to **Check changes**
for a fresh comparison. You still control the mappings, included rows, and
optional relationships. This check derives safety from those choices rather
than imposing a Product- or BOM-specific workflow.

Qualification of larger, multi-level BOM migrations remains part of the
[scalable relationship dependency plan](../../plans/scalable-relationship-dependency-planning.md).
The current related-data limit does not increase until that qualification is
complete.

## What Complete means

Either the reviewed snapshot required no writes, or execution finished and
reconciliation verified the expected Odoo state. A successful HTTP response
alone is not completion evidence.

## What changes and what does not

This is the workflow stage that can create or update Odoo records. It does not
provide whole-migration rollback, save a Recipe, or carry write authority
into another run. Unchanged and blocked rows are not written.

## Needs attention

Do not blindly retry a timeout, connection reset, HTTP 422, or other unknown
write outcome. First inspect the execution journal and reconcile the target.
Retry only through the recorded recovery path after determining which rows, if
any, were applied.

## What makes this work stale

A new source, schema, mapping, reusable-rule, parameter, control,
preparation, comparison, target fingerprint, credential generation, or
dependency order invalidates the execution preview. Return to the earliest
changed stage and regenerate the evidence for this data version.

## Next stage

Keep the execution journal, reconciliation result, and approved review package
with the data project. Resolve any fallout before considering the data version
complete. You may separately save the reusable transformation rules from the
data project overview; that action does not change this load record.

## Related documentation

- [Developer implementation: Load into Odoo](../../developer/workflow/06-load-into-odoo.md)
