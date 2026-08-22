# Migration Projects Phase M4 multi-Recipe Test runs

## Status

**Status:** Implemented on 2026-08-22.

This is the completed M4 implementation record under
[ADR-014](../decisions/README.md#adr-014--migration-projects-coordinate-reusable-recipes-and-cutover-plans).
The [integrated run lifecycle contract](../developer/contracts/integrated-run-lifecycle.md)
is the normative current behavior.

## Delivered boundary

M4 adds Project-owned integrated Test planning. One accepted Test DataVersion
can supply different logical datasets to several exact Recipe revisions. One
run-level target binding, unioned Odoo requirement plan, filtered schema, and
supporting reference bundle serve the run. Every Recipe receives a separate
application and workspace with fresh mapping evidence.

Planning rejects dependency cycles, missing or duplicate dependency edges,
incompatible supporting-reference versions, and overlapping field-level Odoo
writes before provisioning. The browser exposes the plan from the Project
overview and reports integrated progress from bounded registry projections.

## Persistence and recovery

M4 introduced registry generation `impodo-migration-registry-2026-08-m4`.
M5 superseded it with the exact M5 generation; neither generation is upgraded
in place. The M2
DataVersion and workspace-store generations remain current because their
ownership contracts did not change.

The canonical operation intent and one registry transaction reserve the run,
target, plan, applications, workspaces, requirements, and initial issues.
Replay after a registry or workspace-store fault uses the same identities and
does not duplicate any aggregate.

## Compiler reuse

`ProjectRecipeApplicationCompiler` reuses the retained application compiler's
validation and materialization helpers at the new Project-owned boundary. It
creates a normal fresh mapping draft and never calls the superseded
Recipe-owned DataVersion or application-creation workflow. Portable text
normalization becomes a scalar mapping transformation instead of a copied
source column.

## Gate evidence

The focused M4 suite covers:

- two Recipes selecting different datasets from one Test DataVersion;
- one shared target capture and separate application workspaces;
- run-owned reference projection;
- collision and cyclic-dependency rejection before provisioning;
- exact replay after a registry fault;
- bounded integrated progress without opening workspace databases; and
- the representative Customer Recipe through the real compiler and mapping
  service.

Run:

```console
python -m unittest tests.test_migration_project_phase_m4_multi_recipe_runs -v
```

## Deliberate boundary

M4 consumes an already accepted Test DataVersion and reviewed target evidence.
It does not add browser intake for a new Test package or execute applications.
M5 now owns integrated qualification and rollout selection. A fresh Production
run remains outside both phases.
