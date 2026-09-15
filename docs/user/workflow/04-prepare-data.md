---
audience: user
stage: prepare
status: current
---

# Prepare data

## Goal

Apply the confirmed mapping to every frozen row in the current data version,
check data quality, and resolve findings before comparing anything with Odoo.

## Before you start

The exact mapping version must be checked and confirmed. Do not start while a
source, schema, key, or transformation decision is still changing.

## Steps in Impodo

1. Open **Prepare data**.
2. Select **Prepare data** once and follow the progress page.
3. Review row totals, warnings, quarantined rows, and preparation failures.
4. Open **Resolve possible duplicates** when Impodo finds records that may
   describe the same business entity.
5. Merge or keep candidates separate using business evidence.
6. Review prepared-value groups and accept or reject proposed normalization
   decisions when they are present.
7. Approve the resolved prepared data only when no required decision remains.
8. If you want to reuse the revised rules, select **Save reusable rules
   (optional)** and publish the first Recipe or a new Recipe version from the
   data project overview.

![Current prepared-data review inside a fictional data project workspace.](../../images/user/15-prepared-data-review.png)

## How Recipes reuse this work

A Recipe reuses confirmed preparation rules, not a prepared snapshot. Every
data version must run preparation again from its own accepted source and current
mapping confirmation. An Integrated Test creates a separate Recipe work area and mapping draft
for each Recipe in an integrated Test plan; earlier prepared rows are not
copied into it.

This fresh run is mandatory even when the replacement files look identical.
Preparation, quality findings, duplicate decisions, normalization approval,
and content hashes from an earlier data version are historical evidence and
cannot qualify the current one.

## What to check

- Source, prepared, quarantined, and rejected totals reconcile.
- Every source row is accounted for.
- Relationship values resolve to exactly one intended record.
- Duplicate decisions preserve distinct business entities.
- Prepared values still express the source meaning after cleanup.
- Blocking findings are resolved rather than hidden.

When a child record uses its parent as part of its business identity, Impodo
keeps that complete record group together. For example, if a BoM component
does not have a matching product, Impodo sets aside that BoM, its other
component lines, and the missing component line. Other BoMs can still proceed
to review. The source files and Odoo remain unchanged.

## What Complete means

Impodo has a frozen, fully accounted prepared result for the current source,
schema, and mapping evidence. **Final review** becomes available for the
current file data version.

This preparation update preserves complete saved results. You can review a
current saved preparation in the same workspace without creating a replacement
project or selecting your source again. Changed source data, schema, or field
matches still require a fresh preparation.

## What changes and what does not

Preparation saves protected prepared-data evidence. It does not call Odoo, change
the accepted source, modify the Recipe version, or copy prepared rows between
data versions. Publishing the reusable rules is a separate explicit action.
Merge and normalization decisions affect the prepared result, not the original
evidence.

## Needs attention

Investigate blocked rows, unresolved relationships, unexpected quarantine,
count differences, or a stopped background job. A cancelled or failed attempt
may be retried only after its recorded outcome is understood.

**Preparation stopped** means the attempt has ended unsuccessfully. Its
percentage shows the last step reached; waiting will not resume it. After the
cause has been corrected, return to **Prepare data** in the same workspace
and start a fresh attempt. You can keep your accepted source data and saved
field matches.

Direct parent-and-child tables can now use the preparation route for up to
50,000 source rows in the selection. This includes document lines, hierarchies,
and BoM components whose matching rules use an incoming parent as part of
their identity. Impodo still checks the complete group together.

Additional business checks or rules that build a table from other tables may
require a lower limit. If your selection exceeds the supported route, Impodo
stops before preparing the rows and reports that limit. Use a source selection within it.
Restarting the same oversized selection will not change the limit. Your saved
field matches remain available.

For a Recipe run, return to **Review and load** after preparation stops or you
reopen Impodo. If the Recipe already has a complete, current saved result,
Impodo offers its review without preparing the rows again. See
[returning to a Recipe run](../guides/integrated-test-runs.md#needs-attention).

If Impodo reports that it was updated while preparation was starting, restart
Impodo. The stopped attempt did not open the workspace or contact Odoo. Do not
repeat the same attempt from the old browser session.
Save edits in your open tabs before restarting, then reopen the same project
and workspace. You do not need to create a new project to use a preparation fix.

## What makes this work stale

Any change to source evidence, Odoo schema, business keys, mapping version,
parameters, controls, or required resolution invalidates the prepared result.
Run preparation again instead of modifying stored artifacts.

## Next stage

Continue to [Final review](05-final-review.md).

## Related documentation

- [Developer implementation: Prepare data](../../developer/workflow/04-prepare-data.md)
