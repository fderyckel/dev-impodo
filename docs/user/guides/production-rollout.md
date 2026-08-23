---
audience: user
stage: production-rollout
status: current
---

# Production rollout with latest data

## Goal

Apply the exact selected Cutover plan to the complete rollout-day data and a
different compatible Odoo 19 Production database. The Recipe rules come from
the qualified plan. The data, access, checks, approval, load results, and
verification are all new Production evidence.

![The selected integrated qualification is the starting point; Production setup creates fresh data and target evidence instead of promoting this Test result.](../../images/user/04-integrated-qualification.png)

## Before you start

The Project needs one selected integrated Test qualification. Prepare the
complete latest legacy-ERP delivery and know the business cutoff that all its
files represent.

Have two current Production API keys ready:

- a read-only key for Odoo fields, supporting lists, and comparison; and
- a different, limited write key for the exact record types in the plan.

Do not reuse the Test database or treat a successful Test comparison as a
Production check.

## Steps in Impodo

1. Open the data Project and select **Start Production setup**.
2. Name the rollout and enter the latest export cutoff.
3. In the new setup workspace, add the complete latest file delivery.
4. Review every required file and table, then accept the Production data
   version.
5. Connect the Production Odoo 19 database with the read-only key and capture
   its current fields and supporting lists.
6. Return to the Project and select **Continue Production setup**.
7. Enter any values or controls required for this delivery.
8. Enter the separate Production write key and select **Create Production
   applications**.
9. Open each application in the shown dependency order. Prepare, compare,
   approve, load, and verify it as fresh work.

Impodo creates one application workspace for each Recipe in the selected plan.
They share the accepted Production data version and reviewed target identity,
but they do not share mutable mappings, approvals, or results.

## What to check

- The latest data version contains every file and table expected for the
  business cutoff.
- The Odoo database is Production and is not the qualified Test target.
- The read and write keys are different and have only the required access.
- The Cutover plan revision and Recipe revisions match the selected candidate.
- New values, changed columns, missing Odoo fields, missing supporting values,
  and write conflicts are resolved before activation.
- Each Production application starts without Test comparison, approval, load,
  or reconciliation evidence.

## What Complete means

**Active** means Impodo created the Production application workspaces from the
exact selected plan after accepting fresh data and reviewing current
Production access. It does not mean the migration is loaded.

The rollout is complete only after every application has its own approved
comparison, controlled load, and verified reconciliation in dependency order.

## What changes and what does not

Starting setup creates a new Production data version, Production run, and
setup workspace. Activating it creates isolated application workspaces and
records non-secret hashes for the exact Production target and credential
generations.

The selected Cutover plan and Recipe revisions do not change. Test files,
credentials, comparisons, approvals, execution journals, and reconciliation
results are not copied into Production.

## Needs attention

If activation stops, use the recovery action shown by Impodo. Common causes
are an incomplete latest delivery, a changed source structure, a new uncovered
business value, an incompatible Odoo field or supporting value, a rotated key,
or a conflicting write owner.

Correct reusable transformation meaning in authoring and qualify a new plan
revision. Do not add a hidden Production-only rule. Correct delivery-specific
data or access in the Production setup and recheck it.

If an Odoo write response is lost, reconcile the saved journal before retrying
or opening a dependent application. Missing source rows never tell Impodo to
delete or archive Odoo records.

## What makes this work stale

A different selected rollout candidate, changed Recipe or plan meaning,
changed target, rotated read or write key, refreshed schema context, changed
parameters or controls, or a new comparison invalidates the affected
Production readiness. Impodo requires the owning check again and never falls
back to Test evidence.

## Next stage

Open the Production run and work through its Recipe applications in the shown
order. For each application, complete the normal workspace stages from source
review through verified load outcome.

## Related documentation

- [Qualify an integrated Test](qualify-integrated-test.md)
- [Load into Odoo](../workflow/06-load-into-odoo.md)
- [Developer implementation](../../developer/workflow/09-production-rollout.md)
