---
audience: user
stage: setup
status: current
---

# Project and authoring workspace setup

## Goal

Create one governed data project for a migration effort. You do not need a
Recipe to start or finish the work.

## Before you start

For a file project, collect the related CSV or XLSX exports from the legacy
system. For an Odoo-source project, have the exact Odoo 19 address, database
name, and read-only API key.

## Steps in Impodo

1. Open Impodo and select **New project**.
2. Enter a Project name and explain the migration purpose.
3. Choose **Files** or **Data already in Odoo**.
4. Enter a clear source-system name, such as the legacy ERP name.
5. Select **Create project**.
6. On the Project overview, select **Open workspace**.
7. For files, add all related CSV or XLSX exports and select **Use these files
   and continue**. For Odoo data, continue with the read-only source setup.

Impodo creates an Authoring data version and working area for the Project. It
does not create a Recipe, inspect Odoo, or write to Odoo during Project
creation.

![The current New project form separates Project purpose from optional reusable Recipe publication.](../images/user/02-new-project.png)

## Project, data version, workspace, and Recipe

- The **Project** is the migration effort you govern.
- The **DataVersion** is the exact source package used for this authoring work.
- The **workspace** is where you inspect, match, prepare, compare, and load the
  data.
- A **Recipe** is optional reusable rules saved after the mapping is ready.

The Project can contain no Recipe, one Recipe, or several Recipes. Saving a
Recipe never moves the files or DataVersion out of the Project.

## What to check

- The source mode matches the real origin of the data.
- All related source tables are in the same Project when they must be prepared
  or loaded together.
- The source-system label clearly identifies the legacy system or Odoo source.
- The workspace shows the expected Authoring DataVersion number.

## Save reusable rules only when useful

Complete Source data, Odoo data, Match data, and the required preparation and
quality checks first. When the workspace is eligible, return to the Project
overview. You can then:

- continue and complete this as one-off work without a Recipe;
- select **Save as a new Recipe** to preserve reusable rules; or
- publish a new revision of an existing Project Recipe after its rules change.

A Recipe saves logical source shapes, transformations, mappings, relationships,
Odoo requirements, and reusable checks. It does not save source rows, server
addresses, API keys, numeric Odoo record IDs, approvals, or load results.

When several saved Recipes must be checked together, start an
[integrated Test run](guides/integrated-test-runs.md) from the Project
overview. This uses an already accepted Test DataVersion and creates one
isolated application workspace per Recipe. It does not yet execute or qualify
the integrated rollout plan.

## What Complete means

The Project overview lists the Authoring DataVersion and workspace. The
workspace is usable with zero Recipes. Project creation does not load data into
Odoo.

## What changes and what does not

Creating the Project writes the Project identity and its first DataVersion,
run, and workspace. It does not inspect source rows, contact Odoo, publish a
Recipe, approve a load, or write business records.

## Needs attention

If Impodo reports that a form is stale, reopen the current page and review the
latest values. If the source mode is wrong, create a correctly scoped Project
instead of reinterpreting existing evidence.

## What makes this work stale

Project identity does not become stale when later evidence changes. A source
mode or source-system correction that changes what the Project means requires
a new correctly scoped Project during development; do not relabel frozen
evidence.

## Next stage

For a file source, continue to [Source data](workflow/01-source-data.md). For an
Odoo source, continue to [Odoo data](workflow/02-odoo-data.md).

## Related documentation

- [End-to-end training tutorial](tutorials/end-to-end-training.md)
- [Developer implementation](../developer/workflow/00-project-setup.md)
