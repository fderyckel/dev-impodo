---
audience: user
stage: integrated-test
status: current
---

# Integrated multi-Recipe Test run

## Goal

Apply selected Recipe versions to one accepted Test data version and one
reviewed Odoo 19 target. Impodo creates a separate working area for each
Recipe, so their mappings and review evidence never overwrite one another.

![The integrated Test form selects one accepted data version, one reviewed Odoo workspace, exact Recipe versions, and any required order.](../../images/user/03-integrated-test-plan.png)

## Before you start

Your data project needs:

- an accepted Test data version containing the complete current Test export;
- at least one saved Recipe; and
- an Authoring workspace with reviewed Odoo 19 fields and supporting lists.

The current browser starts from an already accepted Test data version. If
the form says that one is required, stop there: do not reuse the Authoring
sample and do not treat independently uploaded files as one complete delivery.
Accepting a new Test delivery is a separate browser step and is not added by
this planning page.

## Steps in Impodo

1. Open the data project and select **Plan integrated Test**.
2. Give the run a clear name.
3. Select the accepted Test data version.
4. Select the Authoring workspace where the Odoo target and supporting lists
   were reviewed.
5. Select the exact current Recipe versions to apply.
6. Under **Required order**, select a dependency only when one Recipe must
   finish and reconcile before another can begin.
7. Select **Start integrated Test**.

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

The run uses one target review and one accepted source delivery. Each Recipe
receives:

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

Starting the plan creates one Test run and a new Recipe work area for
each selected Recipe version. It does not change the accepted data version,
saved Recipes, Authoring workspaces, Odoo data, credentials, or rollout
authority.

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
