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
3. Follow the comparison progress page while Impodo verifies the setup, reads
   Odoo, compares records, builds the review, and saves it. You may leave and
   reopen the page while the current Impodo session remains open.
4. Review totals for **New in Odoo**, **Different from Odoo**, **Already
   matches**, **Needs attention**, and **Set aside**.
5. Inspect field-level differences and relationship resolutions.
6. When Impodo offers **Preview affected groups**, choose the root problems and
   review the complete parent, child, and sibling records that would stay out
   of this load.
7. Select **Set aside affected groups for this load** only when the preview
   leaves a safe remainder. Resolve any remaining or run-wide blocker upstream,
   then prepare and compare again.
8. Download the workbook when you need to review the proposed load in Excel or
   keep a durable rehearsal record.

Before comparison, the prepared-data section can show records that Impodo set
aside. Its cause summary distinguishes a problem on the record itself from a
problem inherited from a linked record. The summary counts only the records on
the current page. Open each row to review all findings and use its source row,
field name, owner, and correction route to fix the cause upstream.

The progress page reports elapsed time during work that has no honest row
percentage. Delayed progress updates do not start another comparison. Selecting
**Compare with Odoo** again while the same workspace already has an active
comparison returns to that attempt.

The affected-group preview is calculated from the saved comparison. It shows
the prepared count, records already set aside during preparation, records that
the new decision would omit, writes remaining, and problems still blocking the
remainder. Accepting the preview makes no Odoo request and applies only to the
current comparison. It does not edit the source, matching rules, or Odoo. If
the comparison or preview has changed, Impodo rejects the old confirmation and
asks you to review it again.

![Reviewing complete affected groups before accepting a reduced load.](../../images/user/16c-review-affected-groups.png)

If Impodo closes before the comparison is saved, the previously current review
remains unchanged. Reopen **Final review** and compare again. A completed report
is published atomically, so an interrupted attempt cannot make a partial review
current.

The workbook opens with **Review overview**, which tells you what will happen,
whether anything needs attention, and what you should do next. Use **Needs
attention** before reviewing **Records to load**. For a file source, **Records
to load** shows the prepared values that Impodo will use. **Changes to Odoo**
shows the new value first and retains the current Odoo value as supporting
evidence. After an affected-group decision, **Deferred issues** retains every
omitted record with its source row and direct or inherited reason.

The workbook uses status words as well as colours. A neutral blank is not a
problem by itself; **Cannot proceed** identifies a record that Impodo will hold
back. Correct source data or rules in Impodo, then prepare and compare again.
Editing a workbook cell does not change the saved data or proposed load.

**Needs attention** is the workbook's action queue. Impodo sorts red **Must
fix** items before amber **Review** items. Each row identifies the source
dataset, record, field, final prepared value, reason, recommended action, and
source row. Use the header filters when you need to focus on one dataset,
field, or priority. Fix every red item in Impodo before loading; review each
amber item and decide whether its upstream field match or source data must
change.

The action queue does not list every safe transformation. **Changed by
Impodo** and **Added by Impodo** remain informational unless the comparison
also records a warning or blocker. This keeps the worklist focused on decisions
that need a data manager.

In **Records to load**, each prepared field keeps only the final value in the
visible cell. The **Prepared value feedback** column identifies the affected
field and uses these statuses:

- **Changed by Impodo** means that a confirmed preparation rule transformed
  the source value.
- **Added by Impodo** means that a confirmed preparation rule supplied the
  value.
- **Review recommended** means that the field has a warning to review in
  Impodo.
- **Needs attention** means that the field has a problem which blocks safe
  progress.
- **Empty but allowed** means that the current comparison found no blocker for
  the blank. Impodo does not treat every blank as an error.
- **As provided** means that Impodo retained the prepared value without a
  recorded change.

A changed or added cell contains an Excel note with the original display value
and the confirmed preparation rule. Open the note when you need supporting
detail. Correct the source value or rule in Impodo and recreate the workbook;
changing the visible cell or its note does not change the migration.

When the source is Odoo, protected business values remain inside Impodo and do
not enter the portable workbook.

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
- Every intended numeric write fits the captured Odoo field precision exactly;
  Impodo does not round a value or choose a rounding method automatically.
- A rerun against refreshed Odoo evidence gives an explainable result.

For an update-only reload of existing records, any nonzero **Create** count is
a hard stop until the identity or target evidence is corrected.

## What Complete means

The current report is **Ready** with no ambiguous or blocked rows, or **Ready
with records set aside** after a reviewed complete-group decision. It remains
bound to the exact prepared and target evidence. At least one safe write must
remain before the load stage can become available for the current data
version.

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
