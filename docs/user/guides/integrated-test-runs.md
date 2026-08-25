---
audience: user
stage: integrated-test
status: current
---

# Integrated multi-Recipe Test run

## Goal

Test selected Recipe versions with a newer source delivery and the Odoo target
you choose for this Test run. Impodo keeps this work inside the same data
project, but creates a fresh Test data version and a separate working area for
each Recipe. The Authoring sample and saved Recipes remain unchanged.

![The Test setup form selects exact Recipe versions, their required order, and the cutoff for the newer delivery.](../../images/user/03-integrated-test-plan.png)

## Before you start

Your data project needs at least one saved Recipe from an accepted Authoring
data version. Have the complete newer Test delivery and the connection details
for your chosen Odoo target ready. Impodo will collect both as fresh Test
evidence.

## Steps in Impodo

1. Open the data project and select **Test with new data**.
2. Select the exact saved Recipe versions to test.
3. Under **Required order**, select a dependency only when one Recipe must
   finish and reconcile before another can begin.
4. Enter the newer data cutoff and select **Create Test setup**. Impodo opens
   **Fresh data** for this run.
5. Review the exact Recipe versions and their required source tables. Expand a
   table only when you need to see its required columns.
6. Select **Add fresh files**, upload the complete newer delivery, review the
   detected tables, and select **Accept Data version**.
7. Under **Check Odoo**, connect the Odoo target for this Test run. Impodo carries the selected
   Recipes' required models into Odoo field discovery, so you do not select
   the target model again.
8. Select **Return to Test run setup**.
9. Under **Review and load**, review the newer data, Odoo target review, read-only access, and
   selected Recipe versions.
10. Select **Create Recipe work areas**.
11. On the run page, select **Continue review and load** for the next Recipe.
12. Prepare, compare, confirm the load, and verify the result.
13. When every application has succeeded in the required order, qualify that
    exact Test run as the Production candidate.

Before creating Recipe work areas, Impodo checks that the Recipe order has no cycle
and that two Recipes do not claim the same writable Odoo field. Reordering two
conflicting Recipes is not a safe repair; one Recipe must own that field.

## What to check

- The Test data version represents one complete, accepted delivery.
- Every selected Recipe version is the intended saved version.
- The required source tables shown under **Fresh data** match the business
  content you expect for each Recipe.
- Dependencies describe real business order, not a workaround for a collision.
- The reviewed Odoo workspace belongs to this data project and target.
- The selected Recipe versions declare non-overlapping writable Odoo fields.

## What Impodo creates

**Create Test setup** creates one draft Test data version, one Test run, and
one shared setup workspace. **Create Recipe work areas** activates that same
run after you accept the newer delivery and review the chosen Odoo target.
Before you add files, **Fresh data** reads the exact selected Recipe versions
and shows their reusable source requirements. Archiving a Recipe later does
not change the version already pinned to this run.
Each selected Recipe then receives:

- its exact saved version;
- only the logical datasets it needs from the Test data version;
- only its required Odoo models, fields, and supporting lists;
- a fresh mapping draft; and
- its own issues and working evidence.

The saved Recipe remains unchanged. No source table or prior workspace is
copied.

The current **Fresh data** page explains what the Recipes require and returns
you to the run after file work. The detailed source review still detects and
confirms the physical tables in the shared setup workspace. Automatic table
matching on the same page is a later part of this refactor.

The setup and Recipe workspaces still keep the detailed evidence. Their browser
navigation belongs to the run: setup permits fresh-data and Odoo-check pages,
while an application permits only preparation, review, load, and verification.
A saved or copied workspace link that belongs to Authoring returns you to the
owning run without changing saved work.

## Ready and Blocked

**Ready** means the Recipe was rebound to the current source and target and a
fresh mapping draft was created without a current blocker. It does not mean
the integrated run is executed or qualified for rollout.

**Blocked** means the run page names the current difference and the next
action. Typical examples are a missing source column, a changed Odoo field, a
missing supporting list, a required parameter, uncovered values, or a quality
scope that must be reviewed again. A blocked application may still contain a
fresh draft when the issue can be reviewed in that workspace.

## What Complete means

All selected Recipes appear in the validated order, each with a distinct
Recipe work area. The run shows one shared Test data version, one shared
Odoo target review, and the exact Cutover plan version created for the run.
Planning alone does not call the Test result qualified.

## What changes and what does not

Starting setup creates fresh Test identities. Activating the setup creates one
new Recipe work area for each selected version. Neither action changes the
saved Recipes, Authoring data version, Authoring workspace, Odoo data, or
rollout authority. Test credentials belong to the shared Test setup and never
become Recipe content.

## Needs attention

If planning stops before workspace creation, correct the named missing
dataset, target field, supporting list, dependency cycle, or overlapping field
owner. If a Recipe application is blocked after creation, return to the run
and select **Continue review and load** for that Recipe. Do not enter its Source
data, Odoo data, or Match data pages and do not save a new Recipe version merely
to hide current-data drift.

## What makes this work stale

A different Test data version, Recipe version, dependency edge, target schema,
supporting-reference version, or credential generation requires a new exact
plan. Earlier Ready status does not transfer.

## Next stage

Complete preparation, comparison, load, and verified read-back from **Review
and load** for each Recipe. Follow dependency order, then
[qualify the integrated Test](qualify-integrated-test.md).

## Related documentation

- [Create a data project](../getting-started.md)
- [Match data](../workflow/03-match-data.md)
- [Prepare data](../workflow/04-prepare-data.md)
- [Developer implementation](../../developer/workflow/07-integrated-test-runs.md)
