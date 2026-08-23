---
audience: user
stage: review
status: current
---

# Final review

## Goal

Compare every eligible prepared row in the current data version with
fresh Odoo evidence and decide whether the proposed outcome is safe to take to
the load stage.

## Before you start

Prepared data must be complete for the current source, schema, and mapping.
Use a reachable Odoo 19 target with the approved read access.

## Steps in Impodo

1. Open **Final review**.
2. Select **Compare with Odoo**.
3. Review totals for **New in Odoo**, **Different from Odoo**, **Already
   matches**, **Needs attention**, and **Set aside**.
4. Inspect field-level differences and relationship resolutions.
5. Resolve every ambiguous or blocked row upstream, then prepare and compare
   again.
6. Download the workbook or technical evidence package when required for the
   rehearsal record.

![Current final comparison with saved rows and per-table Odoo outcomes in one data version.](../../images/user/16-final-comparison.png)

For local Odoo, **Reconnect local Odoo** may appear after Impodo restarts.
Choose the matching setup, then select **Continue comparison** when ready.

![Current local-Odoo reconnect dialog shown before comparison.](../../images/user/16b-local-odoo-reconnect.png)

### If Impodo cannot compare

Final review shows the action that owns the problem:

- **Enter the Odoo read key** or **Replace the Odoo read key** appears only
  when the read credential is missing, rejected, or lacks the required read
  access. Enter the key in the protected form and retry immediately.
- **Review Odoo connection**, **Capture Odoo data**, or **Refresh Odoo data**
  returns you to the Odoo stage when the target details or captured evidence
  needs attention.
- **Review field matches** and **Prepare data again** return you to the stage
  whose saved evidence is no longer current.
- **Try comparison again** appears for a temporary connection failure or an
  incomplete read-only response. It does not ask for a new key.
- An internal storage or unexpected failure shows safe **Support details** and
  does not claim that the Odoo credential is wrong.

Every recovery panel confirms that nothing was changed in Odoo and that your
saved work is unchanged.

## How saving a Recipe relates to this work

Comparison evidence is never part of a Recipe. A Recipe version can preserve
the reusable transformation rules authored in this workspace, but this
data version keeps its source, preparation, target comparison,
approval, and load evidence.

Applying one or several saved Recipe versions to replacement rollout data
still requires a new data version and fresh Odoo evidence.

## What to check

- The target fingerprint identifies the intended Odoo database.
- Create and update totals match the migration purpose.
- Unchanged rows require no write.
- Every changed field is expected and approved.
- No relationship points to an unresolved or ambiguous record.
- A rerun against refreshed Odoo evidence gives an explainable result.

For an update-only reload of existing records, any nonzero **Create** count is
a hard stop until the identity or target evidence is corrected.

## What Complete means

The current report is **Ready** with no ambiguous or blocked rows and remains
bound to the exact prepared and target evidence. The load stage can become
available for the current data version.

## What changes and what does not

Comparison reads Odoo and stores review evidence for this data version. It
does not write to Odoo, save a Recipe, or authorize another run.
Downloading a workbook or review result does not authorize execution.

## Needs attention

Stop for an unexpected create count, duplicate match, blocked relationship,
wrong target fingerprint, or Odoo read failure. Follow the one recovery action
shown on the page. Use **Support details** for the stable failure code; do not
replace a key unless the page specifically asks for one.

## What makes this work stale

Source, schema, business-key, mapping, reusable-rule, parameter, control,
prepared-data, target-evidence, or dependency-order changes require a fresh
comparison. Never load from an older data version's report.

## Next stage

Continue to [Load into Odoo](06-load-into-odoo.md) only for the explicitly
approved target and the exact current review.

## Related documentation

- [Developer implementation: Final review](../../developer/workflow/05-final-review.md)
