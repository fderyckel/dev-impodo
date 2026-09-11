# Impodo

**Prepare clean data. Review every change. Import into Odoo with confidence.**

Impodo is an open-source data transformation and import product built for
**Odoo business analysts, Odoo developers, and Odoo data managers**. It brings
source inspection, field mapping, data quality, relationship handling,
reviewed loading, and result verification into one local browser application.

Turn CSV files, Excel workbooks, or supported Odoo source records into a
repeatable migration workflow. Keep the original delivery intact, explain how
each value becomes an Odoo field, and see what will be created or updated
before you authorize a load. Save reusable rules as **Recipes** when the next
delivery should follow the same decisions.

**Odoo 19 · Python 3.12+ · Windows and macOS · Local data processing**

[Get started](#get-started) ·
[Take the tutorial](docs/user/tutorials/end-to-end-training.md) ·
[Browse the documentation](docs/README.md) ·
[Explore the technical stack](#technical-stack)

![Impodo previews new, changed, and up-to-date records and shows the order in which related records will load.](docs/images/user/17-load-preview.png)

*Review the proposed changes and dependencies before loading. Screenshots use
fictional data; their counts are examples, not performance measurements.*

## Built for the people responsible for Odoo data

A migration needs more than matching spreadsheet headings. Business keys must
identify the right records, selection values must mean the right thing, and
relationships must resolve in the destination database. Impodo makes these
decisions visible and keeps the evidence needed to review them.

| Your role | What you can do with Impodo |
| --- | --- |
| **Odoo business analyst (BA)** | Translate business rules into field matches, selection choices, defaults, and checks. Review proposed values and downloadable Excel workbooks with stakeholders. |
| **Odoo data manager** | Accept source deliveries, resolve data-quality findings and duplicates, reconcile row totals, rehearse migrations, and verify loaded records. |
| **Odoo developer** | Inspect the actual Odoo model and field schema, work with supported standard and custom models, author YAML profiles, and extend a Python application with explicit integration contracts. |

For example, a product workbook may repeat category names and contain bill of
materials (BoM) lines. You can extract reusable category records, separate
parent and child tables, map product identities and relationships, and review
the required load order. The
[training tutorial](docs/user/tutorials/end-to-end-training.md) walks through
fictional customers, products, categories, and BoMs.

## From source delivery to verified Odoo records

Create a data project, then follow the six Authoring workspace stages:

| Stage | What you decide or review |
| --- | --- |
| **1. [Source data](docs/user/workflow/01-source-data.md)** | Inspect files, confirm the source content, and accept the datasets you will use. Odoo-source work has its own capture steps. |
| **2. [Odoo data](docs/user/workflow/02-odoo-data.md)** | Read the permitted Odoo models and fields, then confirm business keys and relevant company or parent scope. |
| **3. [Match data](docs/user/workflow/03-match-data.md)** | Define record identity, field values, transformations, relationships, and which source rows belong in the migration. |
| **4. [Prepare data](docs/user/workflow/04-prepare-data.md)** | Apply the confirmed rules, review quality findings, and resolve duplicate and normalization decisions. |
| **5. [Final review](docs/user/workflow/05-final-review.md)** | Compare prepared records with Odoo and review proposed creates, updates, unchanged records, ambiguities, and blockers. |
| **6. [Load into Odoo](docs/user/workflow/06-load-into-odoo.md)** | Explicitly authorize the reviewed changes, follow execution, and read records back to verify the result. |

Preparation and comparison do not write to Odoo. A changed source, mapping,
schema, or business-key decision requires fresh checks before its results can
be used for loading.

In this README, an **Odoo model** is a record type such as `res.partner`; a
**record** is one instance of that model, and a **field** holds a value or
relationship. A **business key** is the agreed field or combination of fields
that identifies a record, within a company or parent **scope** when needed.
An Odoo **External ID** is a named identifier; it is distinct from the numeric
database **Odoo ID**. Impodo keeps portable matching rules separate from
database-specific numeric IDs.

## What you can do

### Inspect and shape your source data

- **Read CSV and XLSX files.** Inspect CSV encoding, delimiters, headers, types,
  statistics, and warnings. Select Excel worksheets or named tables and check
  their previews before accepting the delivery.
- **Select rows deliberately.** Use guided conditions to include the intended
  population, review exclusions, and keep the complete accepted source intact.
- **Build related tables.** Extract reusable records from repeated values,
  build hierarchies from separate columns, or split repeated-parent tables
  into parent and child datasets with source-row traceability.
- **Capture supported Odoo records.** Define bounded, read-only capture plans
  and preserve the accepted source snapshot for subsequent work.

See [Source data](docs/user/workflow/01-source-data.md) and
[Prepare related tables](docs/user/guides/related-tables.md).

### Map business meaning, values, and relationships

- **Choose how each field gets its value.** Use a source column, combine
  columns, supply a constant or fallback, or explicitly use a verified Odoo
  default where supported.
- **Transform values visibly.** Trim and normalize whitespace, replace text,
  change case, parse locale-specific decimals, round numbers, interpret dates
  and booleans, normalize datetimes to UTC, and apply supported safe formulas.
- **Check business rules.** Validate required values, text length and character
  rules, selection choices, relationships, and supported cross-field checks.
- **Resolve relationships by business key.** Link to incoming records or
  approved existing Odoo records, including scoped identities and fixed
  many-to-one (`Many2one`) references. Configure many-to-many (`Many2many`)
  links and represent one-to-many (`One2many`) relationships through the
  child's inverse `Many2one` field.
- **Review in the browser or Excel.** Inspect rule effects and download a
  matching review workbook with field decisions, issues, proposed values,
  and explanations of transformed cells.

![The Match data editor configures a source value, fallback, whitespace cleanup, and text checks for an Odoo Name field.](docs/images/user/11-mapping-fields.png)

Use the [Match data questions and answers](docs/user/tutorials/match-data-questions-and-answers.md)
for worked examples and the exact meaning of each rule.

### Resolve quality issues and review the load

- **Account for source rows.** Review prepared, excluded, rejected, and
  quarantined outcomes. Quarantine sets affected records aside for review;
  it preserves their evidence.
- **Resolve possible duplicates.** Review candidates and decide whether to
  merge them or keep distinct business entities separate.
- **Approve normalization.** Review proposed groups of equivalent values and
  resolve collisions before proceeding.
- **Compare with the destination.** Distinguish new records, exact-key updates,
  unchanged records, ambiguous matches, and blocked rows. Download the final
  review workbook before approving the proposed load.

### Load, verify, and correct

- **Load the exact reviewed changes.** Impodo uses the Odoo 19 native JSON-2
  API for supported creates and updates, with stable External IDs on supported
  create paths. It loads dependencies in order and completes eligible optional
  relationships in a later pass.
- **Keep a durable execution record.** Impodo journals write attempts and
  stops on an unknown write outcome so it can be reconciled before retrying.
- **Verify the destination.** Read affected records back and download fallout
  details for rows that could not be verified.
- **Correct an eligible verified Authoring load.** Review changes to the
  previously loaded records and apply only the confirmed corrections, with
  zero creates and a new verification result.
- **Transfer between Odoo databases.** Capture supported source records,
  match them against a separate destination, review reuse and creation
  decisions, check transfer order, and explicitly load and verify the transfer.

Read [Load into Odoo](docs/user/workflow/06-load-into-odoo.md) for the distinct
prepared-data, Odoo-to-Odoo transfer, and correction workflows.

### Reuse rules and coordinate migrations

A **data project** holds one migration effort. A **Data version** contains one
accepted delivery of source data. A **workspace** selects datasets from that
delivery and holds the current working evidence. A **Recipe** saves reusable
rules; a **migration run** records what happened when those rules and data
were used.

You can finish a one-off migration without creating a Recipe. For recurring
work, save versioned Recipes and apply them to fresh data through **Fresh
data**, **Check Odoo**, and **Review and load**.

An **Integrated Test run** rehearses several Recipe versions together, with
dependencies such as customers before sales orders. A qualified **Cutover
plan** records the exact versions, order, field ownership, and shared controls
proved by that Test. The implemented file-source **Production run** applies
the selected plan to a fresh delivery and a different compatible Odoo target,
with its own access, comparison, approval, execution, and verification.

Recipes contain no source rows, target credentials, numeric Odoo record IDs,
approvals, or migration results. Test qualification does not authorize a
Production write.

Start with [Impodo concepts](docs/user/concepts.md), then follow
[Integrated Test runs](docs/user/guides/integrated-test-runs.md),
[Test qualification](docs/user/guides/qualify-integrated-test.md), and
[Production rollout](docs/user/guides/production-rollout.md).

## Large-data transformation: measured scope

Impodo uses **Polars, Parquet, and DuckDB** to process data locally, with
columnar execution for supported transformations and separate preparation
workers. The goal is repeatable bulk preparation with reviewable results.

The current supported boundaries depend on the execution path:

| Processing path | Current row boundary |
| --- | ---: |
| One direct dataset, bound to an exact source snapshot, with all transformations supported by the native columnar engine | 100,000 physical rows |
| Direct preparation across multiple datasets, direct relationship routes, or mappings requiring the Python fallback | 50,000 physical rows |
| Derived or materialized preparation | 25,000 physical rows |
| Durable preflight comparison | 25,000 rows |

These are separate stage limits, not a claim that a 100,000-row migration has
been qualified end to end. Retained local Odoo acceptance includes a
150-record load with every row verified and a repeat preview proposing no
writes. Remote throughput requires its own live-target measurement. See the
[acceptance evidence](docs/testing/acceptance.md) and
[remote acceptance runbook](docs/developer/runbooks/remote-odoo-acceptance.md)
for workloads, measurements, and remaining qualification work.

## Get started

Install **Python 3.12 or newer** and open a terminal at the root of this
checkout. These commands create a private environment, install Impodo, and
launch the browser application.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\impodo.exe
```

### macOS Terminal

Use a `python3` command that reports Python 3.12 or newer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/impodo
```

On later starts, run only the launcher command. No environment activation is
required. Impodo opens a single-use authenticated URL on `127.0.0.1` in your
default browser. Keep the terminal open; select **Quit Impodo** or press
`Ctrl+C` to stop it.

For checkout instructions and troubleshooting, use the
[Windows installation guide](docs/user/installation/windows.md) or
[macOS installation guide](docs/user/installation/macos.md). Then
[create your first data project](docs/user/getting-started.md).

The editable installation is for development and evaluation with fictional or
disposable data. For approved internal data, follow the
[internal release runbook](docs/developer/runbooks/internal-release.md).

## Compatibility and operating boundaries

- **Odoo 19 is the supported integration target.** Available models and fields
  depend on the installed Odoo applications, captured schema, permissions,
  and supported mapping and write capabilities.
- **Impodo runs locally on Windows and macOS.** Its browser server binds to
  `127.0.0.1`. Impodo stores project data locally and does not require its own
  PostgreSQL server. Odoo and its database remain separate prerequisites.
- **Local stack assistance is Windows-only.** Impodo can discover and start an
  eligible local Odoo and PostgreSQL stack on Windows. On macOS, start the
  stack separately; see [Connect to local Odoo](docs/user/guides/local-odoo.md).
- **Remote Odoo connections require HTTPS and authorized API access.**
  Prepared-data workflows separate read and load credentials. Odoo-to-Odoo
  transfers use a source-fetch key and a separate destination-transfer key;
  the destination key can write only after explicit load confirmation.
- **Loading is scoped and explicit.** Impodo exposes no generic RPC or direct
  SQL write path, and it provides no whole-migration rollback. Missing source
  rows do not imply deletion or archiving. Production rollout currently
  accepts file-source plans; Odoo-source round-trip Production writes remain
  unsupported.

See [Security and infrastructure](docs/architecture/security-and-infrastructure.md)
for local storage, credential protection, authorization, and deployment
details. Future work is tracked in the
[remaining-work plan](docs/plans/remaining-work.md).

## Technical stack

| Layer | Technology and purpose |
| --- | --- |
| Runtime | **Python 3.12+** runs the application, domain rules, workers, and CLI. |
| Web application | **FastAPI**, **Uvicorn**, and **Pydantic** provide HTTP serving and validated contracts. |
| Browser interface | **Jinja2** renders HTML with local CSS, JavaScript, and Bootstrap Icons. |
| Data transformation | **Polars** executes supported columnar transformations; **Parquet** stores source and prepared data artifacts. |
| Local persistence | **DuckDB** stores project registries, workspace state, and migration evidence. |
| File handling | **openpyxl** handles Excel workbooks; CSV inspection and parsing support governed source acceptance. |
| Protected data and credentials | **cryptography** and **keyring** support protected evidence and local credential storage. |
| Odoo integration | Scoped **Odoo 19 JSON-2** adapters provide native API loading and read-back; dedicated read adapters capture schema and reference evidence. |
| Developer workflows | **PyYAML** supports declarative profiles; **unittest** covers domain, application, adapter, browser-route, architecture, and performance behavior. |

The code separates browser handlers, application workflows, domain rules, and
storage or Odoo adapters. Both browser mappings and YAML profiles compile into
shared migration semantics. Start with the
[architecture overview](docs/architecture/overview.md),
[code organization](docs/architecture/code-organization.md), and
[Python code map](docs/architecture/python-code-map.md).
Dependency requirements live in [pyproject.toml](pyproject.toml); release
locking and verification are documented in the
[release runbook](docs/developer/runbooks/internal-release.md).

## Documentation and resources

| You want to… | Start here |
| --- | --- |
| Complete a guided migration | [End-to-end training tutorial](docs/user/tutorials/end-to-end-training.md) |
| Find a browser task or troubleshoot a stage | [User documentation](docs/user/README.md) |
| Understand Odoo matching rules and edge cases | [Match data questions and answers](docs/user/tutorials/match-data-questions-and-answers.md) |
| Trial your own files | [Scenario trials](docs/user/guides/scenario-trials.md) |
| Author declarative migration rules | [YAML profile authoring](docs/developer/cli/profile-authoring.md) and [example profiles](profiles/) |
| Capture target evidence and compare from the CLI | [Preflight CLI runbook](docs/developer/cli/preflight.md) |
| Explore sample inputs and scenarios | [Examples](examples/), [fixtures](fixtures/), and [scenarios](scenarios/) |
| Understand or extend the implementation | [Developer documentation](docs/developer/README.md) and [contracts](docs/developer/contracts/) |
| Check precise terminology | [Concepts](docs/user/concepts.md) and [glossary](docs/glossary.md) |
| Find architecture, process diagrams, plans, or test evidence | [Complete documentation index](docs/README.md) |

## Contributing

Useful contributions include reproducible Odoo import cases, clearer business
rules and tutorials, adapter improvements, and measured performance work.
Include your Odoo version, relevant installed applications, expected behavior,
and a small fictional dataset when reporting a problem.

Before changing code, read the
[developer setup](docs/developer/setup/windows.md) and
[code-organization rules](docs/architecture/code-organization.md). Run the
focused tests for the affected behavior using the
[acceptance strategy](docs/testing/acceptance.md). Documentation changes follow
the [style guide](docs/style-guide.md) and
[documentation checks](docs/README.md#documentation-maintenance).

## License

Impodo is intended for open-source distribution. This checkout does not yet
include a root license file; the project license must be specified before
redistribution terms can be stated here.
