---
audience: user
stage: load
status: current
---

# Load into Odoo

## Goal

Explicitly load the exact reviewed plan for the current data version
into its approved Odoo 19 target, then verify the recorded outcome.

Impodo currently has two paths at this stage. A prepared-data workspace can
continue through the existing load and verification steps. An Odoo-to-Odoo
workspace can continue from its read-only destination preflight through a
separate preparation, explicit load confirmation, and destination read-back.

## Odoo-to-Odoo transfer

Use this path when the current data version was downloaded from one Odoo 19
database and will be loaded into a different Odoo 19 database. The source key
can only read the source. The destination transfer key belongs only to the
destination. Impodo does not ask for a third key. An Odoo 20 source remains
read-only and cannot continue into this load path.

### 1. Connect the destination

1. After **Download and freeze** is complete, select **Connect destination
   Odoo**.
2. Enter the destination address, database, and destination transfer key.
3. Select **Check and save destination**.
4. Confirm that the page names the correct database and shows **Connection
   complete**.

This check is read-only. It does not create or change an Odoo record.

![A fictional Odoo-to-Odoo project after the destination connection has been checked, with Match destination data as the next action.](../../images/user/19-destination-connection.png)

### 2. Match the frozen records

1. Select **Match destination data**.
2. For each record type, choose the stable business field that identifies the
   same record in both databases. Add another field only when the first field
   can repeat.
3. Select **Check destination matches**.
4. Review the **Reuse**, **Create**, and **Identity issues** totals.
5. Complete every item marked **Needs decision**.

Use a business reference, code, or another stable value. Do not use a numeric
Odoo record ID. For Product Categories, Impodo applies the required name and
parent scope automatically so equal names under different parents remain
separate.

The results distinguish three types of field handling:

- **Put aside for this transfer** is your decision not to write that captured
  field.
- **Managed by destination Odoo** means that Odoo rebuilds the field. Impodo
  excludes it from writes and verification automatically; you do not need to
  decide what to do with it.
- **Complete values for new records** asks you to confirm a value that is
  needed only when a new destination record will be created.

![The current destination-matching result separates reuse and create totals from a field that destination Odoo manages automatically.](../../images/user/20-destination-matching.png)

If an identity is blank or repeated, choose another stable identifier, correct
and recapture the source, or select **Put this source record aside**. Putting a
record aside excludes that whole record from this transfer. Putting a field
aside excludes only that field from writes. Neither action changes source
Odoo or the frozen evidence.

### 3. Review the order and exact transfer

1. In Stage 5, select **Validate transfer order**.
2. Confirm that supporting records appear before records that use them.
3. Select **Review transfer**.

![The current transfer-order result shows the approved waves and keeps destination writes disabled.](../../images/user/21-transfer-order.png)

A **wave** is one group of record types that Impodo can load together. Impodo
calculates the waves from the saved relationships; you do not arrange them by
hand.

For each record type, choose one policy:

- **Reuse existing, create missing** reuses a unique match and creates only a
  missing record.
- **Reuse existing only** stops the transfer when a record is missing.
- **Update existing, create missing** permits the reviewed fields on existing
  matches to change and creates missing records.

Select **Build review package**. Check the source count, reuse count,
create count, fields to write, fields and records put aside, create-only
values, and load order. Add an optional approval note, select the approval
checkbox, and then select **Approve exact transfer package**.

![The current frozen transfer package shows the exact policy, counts, fields, and create-only value source before approval.](../../images/user/22-transfer-review.png)

### 4. Run the final read-only check, then load once

1. In Stage 6, select **Run read-only preflight**.
2. Confirm the destination database and compare the current reuse, create,
   field, and relationship totals with the approved package.
3. Continue only when the page shows **Preflight passed**.

![The current destination preflight names the database and makes clear that the check cannot write to Odoo.](../../images/user/23-transfer-preflight.png)

The safety notice calls this check **8A**. For you, it is the read-only
preflight at the start of Stage 6.

4. Select **Prepare exact destination load**, then **Prepare exact load
   confirmation**. This performs one last read and still cannot write.
5. On **Confirm and load**, check the database, record totals, relationship
   fields, and wave order. If every record is reused, Impodo offers no load
   action because no write is needed.
6. Select **Load ... into destination Odoo** once. This is the first action in
   this path that can write.
7. Wait for **Verify result**. A transfer is complete only when Impodo reads
   the destination back and verifies the result.

If the destination changed after approval, Impodo stops before loading. Return
to **Match destination data**, rebuild and approve the transfer package, then
run preflight again. If a write was interrupted, do not start a second load.
Use **Assess and resume interrupted transfer** or **Verify what happened in
Odoo**, whichever the saved outcome page offers.

## Before you start

The current final review must be **Ready**. Confirm the data version purpose,
target, exact write totals, dependency order, writable fields, and required API
key. This key authorizes only the reviewed target operation; it does not grant
authority to another data project, data version, or future rollout.

## Steps in Impodo

The following steps apply to a prepared-data workspace. Use the Stage 6
sequence above for an Odoo-to-Odoo workspace.

1. Open **Load into Odoo**, then review **Check changes**.
2. Confirm the target, exact snapshot, new and changed totals, field scope, and
   the compact **What Impodo will load first** order.
3. Continue to **Confirm and load**.
4. If Stage 2 did not save a loading key, enter an API key approved for this
   load. If a loading key is available, Impodo uses it without asking you to
   enter it again.
5. Read the explicit confirmation and select the single load action once.
6. Follow the current load group and relationship-completion totals. Do not
   resubmit an uncertain request.
7. Open **Verify result** to read back the affected records.
8. Review reconciliation when any row cannot be verified. A read-back
   difference means Odoo accepted the record but stored at least one different
   value; it does not mean that Odoo rejected the row. Use **See the different
   values** to filter by field or find a prepared row, Odoo record, or value.
   For a BOM line, the card also shows the reviewed BOM and component keys.
   Compare the prepared and Odoo values and read **What to check** before
   deciding whether the change matters. When a BOM line's prepared Sequence is
   blank and Odoo returns 0, the card counts those lines. Confirm that 0 gives
   the intended line order; the read-back difference alone does not mean the
   component is missing. Select **Re-check Odoo now** for a
   fresh read-only attempt, or download the highlighted source workbook to
   locate the original worksheet cells. The original source workbook is never
   changed. Do not load the same review again.

![Current Check changes screen with exact new, changed, up-to-date, and per-table totals.](../../images/user/17-load-preview.png)

![Current Confirm and load screen with the optional loading-key field and one explicit load action.](../../images/user/17b-load-confirmation.png)

![Current fallout outcome with the different-values card, field filter, prepared and Odoo values, and read-only re-check.](../../images/user/18-load-fallout.png)

## Correct a verified Authoring load

When an eligible Authoring load is fully verified, its data-project page shows
**Correct this Odoo load**. The original load and workspace become historical
evidence; Impodo creates a separate correction workspace over the same data.

1. Select **Correct this Odoo load**, then **Start correction**.
2. Select **Edit correction rules**.
3. Change only the rule that was wrong. This can be a source-to-field value
   rule, a Selection choice, a constant or fallback, or trimming and casing
   behavior. The same editor also lets you inspect relationship matches.
4. Return to the correction page and select **Review correction**.
5. Review the compact counts by dataset, Odoo model, and field. The review
   always shows zero creates.
6. If there are no blockers, explicitly confirm and select **Apply N
   corrections**.
7. Leave the progress page open or return to it later. Impodo rereads the exact
   affected records, applies only the reviewed fields, and verifies the
   outcome automatically.

Impodo compares the previous prepared intent, the current Odoo value, and the
corrected prepared intent. It does not rerun the whole migration and does not
search for another target by business key. If the corrected result is already
present in Odoo, the page shows that no write is needed.

Impodo can also correct a many-to-one choice when both the previous choice and
the corrected choice each match exactly one existing Odoo record. For example,
you can correct 37 Products from a mistaken `UNI` Unit choice to the existing
standard `Unit` record. Impodo changes only each Product's Unit field. It does
not create, rename, merge, or otherwise change a Unit of Measure record.

Matching remains case-sensitive. `Kg`, `kg`, and `KG` stay different unless
you explicitly confirm another rule in **Match data**. A missing or duplicate
relationship match, a move to another target field, a missing Product, or a
concurrent Odoo change stops the whole correction before writing.

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
- The first visible load groups put supporting records before records that use
  them. The summary shows at most five groups and reports how many later groups
  follow.
- The journal records every attempted row.
- Read-back verification accounts for the final outcome.
- A weight or amount with more decimal places than the captured Odoo field can
  store is resolved before confirmation. Impodo stops the load rather than
  silently rounding it or guessing a unit conversion.

When **Load unavailable** names `TARGET_NUMERIC_PRECISION_LOSS`, choose one
explicit recovery:

- Select **Refresh Odoo precision** after an Odoo developer changes the field
  to the required precision. In **Odoo data**, check for Odoo changes and use
  the updated details. Then submit the field matches, prepare, approve, and
  compare again.
- Select **Adjust the number rule** only when the business accepts a documented
  rounding rule. In **Match data**, open the affected field, set **Decimal
  rounding**, its places, and method, review the impact, then prepare, approve,
  and compare again.

Changing the Odoo field precision is the non-lossy option when the prepared
values are correct. A unit conversion is a separate business rule and is safe
only when the source unit and Odoo unit are both explicitly known.

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

**Check changes** expresses that frozen order as plain numbered load groups.
Each group names at most three prepared record types and a record count; later
groups are summarized rather than expanded into a very long list. This is an
explanation of the current reviewed mappings, not a fixed Product or BOM
workflow. You keep the freedom to change included rows, business keys,
mappings, and optional relationships, then compare again to produce another
safe order.

When loading starts, the progress page names the current group. A record is
counted as having a final write result only after Impodo has saved its Odoo
outcome. An in-flight call or a row waiting for its reviewed relationship does
not count as finished. If optional relationships need a second pass, the page
shows how many affected new records are finished and how many remain.

Immediately before loading, Impodo checks existing Odoo records and related
records in bounded groups. A reviewed key must still point to the same unique
record. If it was removed, became ambiguous, or now points somewhere else,
Impodo creates no load journal and writes nothing; return to **Check changes**
for a fresh comparison. You still control the mappings, included rows, and
optional relationships. This check derives safety from those choices rather
than imposing a Product- or BOM-specific workflow.

Larger, multi-level BOM migrations remain part of the
[deferred scale qualification](../../plans/remaining-work.md#1-qualify-related-and-mixed-preparation-at-100000-rows).
The current related-data limit stays in place while that work remains open.

## What Complete means

Either the reviewed snapshot required no writes, or execution finished and
reconciliation verified the expected Odoo state. A successful HTTP response
alone is not completion evidence.

For the Odoo-to-Odoo path, **Preflight passed** completes only the read-only
preflight within Stage 6. **Load destination Odoo** becomes complete only after
the confirmed load has a verified destination read-back. A prepared confirmation or accepted
Odoo response alone is not completion evidence.

For a completed-load correction, Complete means its automatic exact-record
read-back is verified. A submitted correction request or accepted API response
is not completion evidence.

## What changes and what does not

This is the workflow stage that can create or update Odoo records. It does not
provide whole-migration rollback, save a Recipe, or carry write authority
into another run. Unchanged and blocked rows are not written.

## Needs attention

When relationship planning blocks the load, **Check changes** groups equivalent
issues by reason and record type. Each message states why those records cannot
load and the next business action, such as adding a missing supporting record,
choosing a unique key, or making a relationship optional. Resolve the listed
issue in the earlier workflow, then compare again. Support details remain
bounded and do not expose source values or internal row identifiers.

Do not blindly retry a timeout, connection reset, HTTP 422, or other unknown
write outcome. First inspect the execution journal and reconcile the target.
For an Odoo-to-Odoo transfer whose page shows **Interrupted transfer**, select
**Assess and resume interrupted transfer**. Impodo uses the same destination
transfer key to read the exact affected fields before it continues the same
saved journal. It can retry an interrupted create only when no matching record
exists, and it keeps the same External ID so the retry cannot silently create
an unrelated duplicate. If the saved outcome is terminal rather than
interrupted, use **Verify what happened in Odoo**; do not submit the transfer
again.

For a prepared-data load interrupted when Impodo or your computer stopped,
open **Review interrupted load**, then select **Assess and resume interrupted
load**. The page shows accepted records, records waiting to load, uncertain
requests, and partially applied records. Impodo checks uncertain requests and
the earlier records that the remaining work depends on. It then continues the
same saved load with the original loading key and batch size. Impodo reads all
loaded records after the remaining work before it can report a verified result.
Keep the workspace and its matching rules intact while recovering; the
original preview totals do not show how many records remain.

If Odoo access changed and you keep the new access, refresh the Odoo fields,
confirm the prepared review, and compare again. **Confirm and load** then
explains that an earlier upload was interrupted. Its load action checks the
saved receipts and closes the earlier attempt before loading the fresh
comparison. The original records and receipts remain saved.

![Interrupted prepared-data load with saved counts and an explicit assessment and resume action.](../../images/user/18b-load-recovery.png)

For an Odoo-to-Odoo transfer, Impodo checks every earlier completed group before
it continues. For a prepared-data load, it checks the earlier records needed by
the remaining work. If a created record is waiting for an optional relationship,
recovery writes only the relationship fields that were already reviewed. A
changed required record, ambiguous record, missing receipt, changed key, or
changed loading identity stops recovery and requires a new review. The final
read-back can also find a change in an earlier record that was not needed to
resume. In that case, the result needs attention; Impodo cannot undo the new
Odoo writes automatically.

If the load is complete but the newest verification shows fallout, do not
reload the records. Use **Re-check Odoo now** after correcting Odoo or the
affected exact records. The previous verification remains as history and the
new read becomes the current result. The highlighted workbook is a local
derivative containing business values; it is access-controlled and
integrity-checked inside the project, while API keys remain in the operating-
system credential vault.

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
