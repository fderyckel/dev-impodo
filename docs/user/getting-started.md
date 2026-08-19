---
audience: user
stage: setup
status: current
---

# Recipe and data-version setup

## Goal

Create one reusable migration Recipe and register the people, representative
source, target, purpose, and retention information for authoring data version
1.

## Before you start

Have the source owner, data manager, functional owner, source system, export
date, classification, retention period, and intended Odoo 19 target ready. For
a file project, collect the related CSV or XLSX files for the same migration.

## Steps in Impodo

1. Open Impodo and select **New Recipe**.
2. Choose whether the source is files or existing Odoo records.
3. Enter the Recipe's authoring data-version and governance details.
4. Configure the Local or Remote Odoo target.
5. For a file source, add the related CSV or XLSX files.
6. Review the complete setup.
7. Select **Register project** to finish the contained authoring workspace.

![Current new-project screen with file and Odoo source choices.](../images/user/02-create-project.png)

Keep related tables in one Recipe when they must be loaded in dependency
order. Do not create one Recipe per Odoo record type.

![Current data-version overview showing the six stages for one fictional migration.](../images/user/03-project-overview.png)

## What to check

- The source mode matches the real origin of the data.
- The target URL and database identify the intended Odoo 19 environment.
- The data manager and functional owner are real responsibilities, not generic
  placeholders.
- Classification, purpose, and retention match the data being handled.
- File-source projects contain the intended exports.

## What Complete means

The data-version overview opens, the setup is registered, and Impodo shows the
first available workflow action. Registration does not publish the Recipe or
load data into Odoo.

## What changes and what does not

Registration saves an auditable contained workspace boundary. It does not edit
a source file, publish reusable Recipe meaning, read business records from
Odoo, or write to Odoo.

For file data versions, an incorrect source file can still be replaced before the
first table selection is frozen. The file is never edited in place.

## Needs attention

Return to the relevant setup page when Impodo reports missing ownership,
governance, source, or target information. If the Recipe was created for the
wrong source mode or business purpose, create a correctly scoped Recipe
instead of trying to reinterpret its evidence.

## What makes this work stale

Draft changes increment the workspace revision. A form opened before another
change may be rejected as stale; reopen it and review the current values.
Registered business and target setup is not silently rewritten by later
workflow stages.

## Next stage

For a file source, continue to [Source data](workflow/01-source-data.md). For an
Odoo source, continue to [Odoo data](workflow/02-odoo-data.md) to choose the
record type and eligible fields.

## Related documentation

- [End-to-end training tutorial](tutorials/end-to-end-training.md)
- [Developer implementation: Recipe and data-version setup](../developer/workflow/00-project-setup.md)
