# Impodo documentation

Impodo helps you prepare, check, and import data into Odoo 19 and Odoo 20.
These guides are for Odoo users, data managers, and data assistants working
through an import, checking a result, or preparing the next delivery of data.

## Start here

| What you want to do | Where to start |
| --- | --- |
| Install and open Impodo. | Follow the [Windows installation guide](user/installation/windows.md) or [macOS installation guide](user/installation/macos.md). |
| Begin your first import. | [Create a data project](user/getting-started.md), choose your source, and open its workspace. |
| Practise before using your own data. | Follow the [guided training tutorial](user/tutorials/end-to-end-training.md) with fictional customers, products, and bills of materials. |
| Understand how your work is organized. | Read [How Impodo organizes your migration](user/concepts.md) for an explanation of projects, data versions, workspaces, and reusable Recipes. |
| Move records from one Odoo database to another. | Follow the [Odoo-to-Odoo transfer guide](user/workflow/06-load-into-odoo.md#odoo-to-odoo-transfer). |

For the full list of guides, open the [user documentation](user/README.md).

## Follow your import

After creating a project, use its workspace to prepare and review the import.
For a file source, follow these six stages in order. If you are returning to
saved work, open the guide for the stage you are on.

| Stage | What you do |
| --- | --- |
| 1. [Source data](user/workflow/01-source-data.md) | Add your files, check the detected tables and columns, and choose the data to use. |
| 2. [Odoo data](user/workflow/02-odoo-data.md) | Connect to Odoo and review the record types and fields available for your import. |
| 3. [Match data](user/workflow/03-match-data.md) | Choose the company and rows to use, identify records, and match source columns and linked values to Odoo. |
| 4. [Prepare data](user/workflow/04-prepare-data.md) | Review the prepared records and resolve data issues before approving them. |
| 5. [Final review](user/workflow/05-final-review.md) | Compare the prepared data with Odoo, review proposed changes, and address records needing attention. |
| 6. [Load into Odoo](user/workflow/06-load-into-odoo.md) | Confirm the load, follow its progress, and check the result in Odoo. |

For an Odoo source, the first stages guide you through connecting to the source
database and selecting its records. Use the
[Odoo-to-Odoo transfer guide](user/workflow/06-load-into-odoo.md#odoo-to-odoo-transfer)
for the labels and steps shown in that workflow. Transfers use the same Odoo
major version: Odoo 19 to 19, or Odoo 20 to 20.

If a stage shows **Needs attention**, its guide explains what to check and how
to continue. The [Final review guide](user/workflow/05-final-review.md) helps
you interpret comparison results. The [Load into Odoo guide](user/workflow/06-load-into-odoo.md)
covers rejected records and interrupted loads.

## Find help for a particular task

- **Choose matching rules.** Use [Match data: questions and answers](user/tutorials/match-data-questions-and-answers.md)
  for examples of record identifiers, field matches, and linked values.
- **Work with related tables.** Read [Prepare related tables](user/guides/related-tables.md)
  when information is spread across files or repeated across rows.
- **Connect to Odoo running on your computer.** Follow the
  [local Odoo guide](user/guides/local-odoo.md) for connection checks and
  troubleshooting.
- **Keep clean Odoo records for reuse after a database reset.** Follow
  [Save clean Odoo data and restore it after a reset](user/guides/saved-odoo-data.md).
- **Reuse import rules with a new delivery of data.** Start with the
  [Recipe guidance](user/getting-started.md#save-reusable-rules-only-when-useful),
  then use [Test with new data](user/guides/integrated-test-runs.md) to check
  selected Recipes together.
- **Prepare a production migration.** [Qualify your integrated Test](user/guides/qualify-integrated-test.md)
  before following [Production rollout with latest data](user/guides/production-rollout.md).
- **Understand an unfamiliar term.** Look it up in the [glossary](glossary.md).

## Get help while working in Impodo

Select the question mark beside a page title to open its guide in a new tab.
Question marks beside individual choices open the relevant explanation.
You can also open **Help and guides** in the sidebar to browse the user guides.

## For developers and contributors

The [developer documentation](developer/README.md) covers installation for
development, implementation details, command-line tools, and testing.

<details>
<summary>Technical references and documentation maintenance</summary>

### Architecture

- [Architecture overview](architecture/overview.md) explains how Impodo's
  components work together.
- [Code organization](architecture/code-organization.md) and the
  [Python code map](architecture/python-code-map.md) help you find the code
  responsible for a feature.
- [Security and infrastructure](architecture/security-and-infrastructure.md)
  describes data handling, access controls, and deployment requirements.
- [Architecture decisions](decisions/README.md) records accepted design choices.

### Documentation maintenance

Follow the [documentation style guide](style-guide.md). Write the user's goal,
choices, and next action first. Keep implementation detail in developer pages.

When workflow behavior changes, update its user and developer guides together
with the [workflow registry](workflow.yml), affected contracts, code
documentation, screenshots, and tests. Accepted contracts define required
behavior; verify discrepancies before changing a guide or its implementation.

Run these checks from the repository root using the checkout's environment:

```console
uv run --frozen --group quality python scripts/documentation_quality.py --check --report
uv run --frozen --group quality python scripts/code_documentation_inventory.py --check
uv run --frozen --group quality python scripts/code_documentation_inventory.py --missing
uv run --frozen --group quality python -m unittest tests.architecture.test_documentation_quality tests.architecture.test_code_documentation
uv run --frozen --group quality python scripts/check_repository_quality.py
```

Run `vale docs` for advisory spelling and language feedback. Review each alert
for meaning and preserve exact labels, example values, and identifiers.

</details>
