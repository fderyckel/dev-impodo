---
audience: user
stage: integrated-test
status: current
---

# Integrated multi-Recipe Test run

## Goal

Test selected Recipe versions with a newer source delivery and a
pre-production Odoo 19 server. Impodo keeps this work inside the same data
project, but creates a fresh Test data version and a separate working area for
each Recipe. The Authoring sample and saved Recipes remain unchanged.

![The Test setup form selects exact Recipe versions, their required order, and the cutoff for the newer delivery.](../../images/user/03-integrated-test-plan.png)

## Before you start

Your data project needs at least one saved Recipe from an accepted Authoring
data version. Have the complete newer Test delivery and the pre-production
Odoo connection details ready. Impodo will collect both as fresh Test evidence.

## Steps in Impodo

1. Open the data project and select **Test with new data**.
2. Select the exact saved Recipe versions to test.
3. Under **Required order**, select a dependency only when one Recipe must
   finish and reconcile before another can begin.
4. Enter the newer data cutoff and select **Create Test setup**.
5. Upload, review, and select **Accept Data version** for the complete newer
   delivery.
6. Connect the pre-production Odoo server. Impodo carries the selected
   Recipes' required models into Odoo field discovery, so you do not select
   the target model again.
7. Return to the data project and select **Continue Test setup**.
8. Review the newer data, pre-production Odoo review, read-only access, and
   selected Recipe versions.
9. Select **Create Recipe work areas**.
10. Review current-data differences in each fresh Recipe work area.
11. Prepare, compare, confirm the load, and verify the result.
12. When every application has succeeded in the required order, qualify that
    exact Test run as the Production candidate.

Before creating Recipe work areas, Impodo checks that the Recipe order has no cycle
and that two Recipes do not claim the same writable Odoo field. Reordering two
conflicting Recipes is not a safe repair; one Recipe must own that field.

## What to check

- The Test data version represents one complete, accepted delivery.
- Every selected Recipe version is the intended saved version.
- Dependencies describe real business order, not a workaround for a collision.
- The reviewed Odoo workspace belongs to this data project and target.
- The selected Recipe versions declare non-overlapping writable Odoo fields.

## What Impodo creates

**Create Test setup** creates one draft Test data version, one Test run, and
one shared setup workspace. **Create Recipe work areas** activates that same
run after you accept the newer delivery and review the pre-production target.
Each selected Recipe then receives:

- its exact saved version;
- only the logical datasets it needs from the Test data version;
- only its required Odoo models, fields, and supporting lists;
- a fresh mapping draft; and
- its own issues and working evidence.

The saved Recipe remains unchanged. No source table or prior workspace is
copied.

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
owner. If a Recipe work area is blocked after creation, open only that work
area's issue and fresh draft; do not save a new Recipe version merely to
hide current-data drift.

## What makes this work stale

A different Test data version, Recipe version, dependency edge, target schema,
supporting-reference version, or credential generation requires a new exact
plan. Earlier Ready status does not transfer.

## Next stage

Complete matching, preparation, comparison, load, and verified read-back in
each Recipe work area. Follow dependency order, then
[qualify the integrated Test](qualify-integrated-test.md).

## Related documentation

- [Create a data project](../getting-started.md)
- [Match data](../workflow/03-match-data.md)
- [Prepare data](../workflow/04-prepare-data.md)
- [Developer implementation](../../developer/workflow/07-integrated-test-runs.md)
