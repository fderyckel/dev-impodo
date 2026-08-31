# Impodo

Impodo is a local browser platform for preparing CSV and XLSX data for an Odoo
19 migration. It helps a data manager govern the source files, capture the
target Odoo schema, and build a validated mapping before any migration work is
considered.

Impodo's normal preparation and comparison workflow is read-only. For a
disposable local or remote Odoo 19 target, it can preview and explicitly load
a reviewed, schema-bound set of standard or custom models and writable fields
through Odoo's native API, then read the written records back and show
actionable fallout.

## The platform

Impodo runs locally on Windows and macOS and opens in the default browser on a
local-only `127.0.0.1` address. A Project is the business and governance root.
Each Data version owns its source package, and each contained workspace keeps
its own mapping, validation, preparation, comparison, and load evidence in
isolated local DuckDB stores.

The platform accepts `.csv` and `.xlsx` source files. It can connect to an
authorised Odoo 19 target. A local Windows instance uses an explicitly
selected `odoo.conf` and fixed read-only metadata operations without an Odoo
API key. A remote read connection requires HTTPS and a dedicated read-only API
key. An explicit local or remote load requires a separately authorized API
key.
Impodo does not classify targets by an organisation's lifecycle stages.

The browser, source inspection, and read-only Odoo connection work on both
operating systems. The in-browser assistant that discovers and starts a local
Odoo and PostgreSQL stack is currently available on Windows only. On macOS,
start a local Odoo stack separately before connecting to it in Impodo.

## What Impodo does today

### Project setup

- Records the migration context, responsible people, data classification, and
  retention details.
- Selects governed CSV/XLSX files or existing Odoo records as the source mode.
- Adds and hashes source files for file-mode projects. Odoo-source projects can
  register without an export date or placeholder file, proceed to read-only
  model/field discovery, and save a bounded scalar capture plan. Saving that
  plan does not contact Odoo or freeze records; live row capture remains the
  next slice.
- Configures and optionally tests the read-only Odoo connection.
- Keeps read and write keys in separate target-bound vault roles and browser
  fields. Loading and read-back never fall back to the setup read key.
- Records non-secret removal receipts when target changes or project deletion
  remove stored target credentials.

### Source discovery and dataset freeze

- Inspects CSV encoding, delimiter, headers, column types, statistics, and
  warnings.
- Inventories XLSX worksheets and named tables, with bounded previews and
  source-file safety checks.
- Lets the user confirm the selected source content and freeze it as named
  datasets. The frozen datasets remain bound to the confirmed source hashes.
- For an Odoo-source project, saves append-only bounded capture-plan revisions
  bound to the current authenticated schema identity: one model, at most 50
  eligible scalar fields, active/archive policy, and at most 10,000 rows. The
  browser distinguishes this plan from the later live read and immutable
  snapshot publication.
- Binds every Odoo capture plan to one executable policy covering Tier-1
  fields, limits, protected-data handling, connection-only target assurance,
  and the explicit `PRODUCTION_WRITE_UNSUPPORTED` native JSON-2 disposition.

### Target schema and governed mapping

- Captures the permitted Odoo models and their fields through read-only
  metadata calls.
- Stores verified model and effective-field snapshots in the project DuckDB
  database, so reopening and mapping do not automatically contact Odoo.
- Binds those catalogues to a non-secret read-credential generation hash. This
  records Impodo-side key rotation without claiming Odoo principal identity.
- For remote reads, verifies the API key's own Odoo user and required model-
  level read access through a closed probe, including a bounded active-company
  scope check, then binds non-secret principal, observed-permission, and context
  hashes to model/schema evidence. Raw Odoo user, group, and company IDs are
  not stored.
- Records the target business keys and any company or tenant scope fields.
- Maps each frozen dataset to an Odoo model and its writable scalar fields.
- Lets each scalar field use a source column, constant, source fallback, or an
  explicit leave-unset/Odoo-default policy.
- Applies allowlisted trim, whitespace, empty-to-null, find/replace, casing,
  locale-aware decimal and explicit rounding, date-format, boolean, UTC
  datetime, and safe formula transformations, with bounded raw-to-proposed
  previews.
- Authors plain-language exact-length and first/last/whole-value character
  checks, while keeping bounded custom patterns behind an optional advanced
  control.
- Configures many2one and many2many relationships using governed business
  keys; one2many relationships are handled through the child inverse field.
- Authors hash-bound derived-entity rules that assign deterministic,
  related-entity-owned IDs to reusable values found in denormalized source
  fields, with bounded alias and hierarchy previews; extracted datasets appear
  beside the original rows in Mapping and are materialized during readiness.
- Prepares repeated-parent source tables as guided parent and child logical
  datasets, carries every source row into the child dataset, and offers both
  datasets to Mapping with safe inverse-many2one guidance.
- Creates immutable mapping revisions, validates them, and allows submission
  of the exact validated revision after blocking findings are resolved.

Changing a confirmed source, frozen dataset, Odoo schema capture, or governed
business key invalidates the active mapping so it must be validated again.

### Practical Odoo load

- Freezes the exact compared rows and field intentions automatically.
- Shows create, update, and unchanged totals before any write.
- Requires one explicit **Load into Odoo** action.
- Requires a separately supplied or stored write key for load and read-back;
  the read-only setup key cannot authorize execution.
- For remote loads, probes that key independently for read-back access to the
  exact reviewed model scope and write access only to models with reviewed
  write fields. The journal binds non-secret credential-generation, principal,
  observed-permission, and context hashes; read-back re-probes them.
- Audits successful read/write credential storage and replacement using only
  the safe random binding hash and storage class, never the key or raw Odoo
  identity values.
- Derives an exact per-preview JSON-2 capability from the captured schema and
  confirmed mapping, uses dependency-ordered batches and exact business-key
  updates, and exposes no direct SQL or generic RPC.
- Journals every proposed write and stops without retrying after a lost write
  response.
- Reads accepted rows back by Odoo ID, re-matches uncertain responses by the
  governed business key, and shows verified rows or downloadable fallout.

**Delivery status:** The bounded preparation, review, durable preflight,
execution snapshot, practical load, and read-back reconciliation path are
implemented. A live 150-row disposable-target run verified every row and
repeated with no proposed writes or duplicates. The first remote Odoo 19 path
supports bounded scalar creates, incoming many2one references to earlier
imports, exact-key many2one references to existing target records, stable
External IDs, remote many2many creates, and exact-key scalar or relationship
updates. Create-time cycles made only of deferrable relationships use a
reviewed two-phase create-then-ORM-update path; identity/scope cycles and
fields required during create remain blocked. Incremental relationship
commands, retained live-target throughput evidence and any measurement-led
tuning, and production cutover controls remain later delivery scope.

The opt-in [remote Odoo 19 acceptance run](docs/developer/runbooks/remote-odoo-acceptance.md)
is ready for a disposable on-premises database. It exercises 150 sanitized
rows through the real remote writer and read-back path and records observed
throughput; live evidence still requires the target server.

## Install and start

Impodo requires Python 3.12 or newer.

### Windows

Install 64-bit Python 3.12 or newer first. The standard Windows Python
installation includes `venv`; no separate virtual-environment package is
required.

For the first setup, open PowerShell at the repository root, create the local
`.venv`, and install Impodo into it:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

The `.venv` directory now contains this checkout's isolated Python environment
and the Impodo launcher. Start Impodo with:

```powershell
.\.venv\Scripts\impodo.exe
```

You do not need to activate the virtual environment because these commands use
its executables directly. On later starts, only run the launcher command.

The launcher opens a single-use authenticated URL in the default browser. To
stop Impodo, use **Quit Impodo** in the browser or press `Ctrl+C` in the
PowerShell window.

### macOS

For the complete GitHub-checkout installation, library verification, and
launch instructions, see [Install Impodo on macOS](docs/user/installation/macos.md).
Impodo requires Python 3.12 or newer. From the checkout, start it with
`.venv/bin/impodo`. The launcher stores projects under
`$HOME/Library/Application Support/Impodo/projects` by default. Keep the
Terminal window open while using Impodo; press `Control+C` or select **Quit
Impodo** in the browser when finished.

Editable installation is the development lane. For use with approved internal
data, promote and install a clean, evidence-producing bundle by following the
[internal development and release runbook](docs/developer/runbooks/internal-release.md).

## Documentation

For the complete documentation, choose the data-manager or developer path at
[docs/README.md](docs/README.md). That index also links the operating runbooks,
contracts, architecture, plans, and test evidence.
