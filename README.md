# Impodo

Impodo is a local browser platform for preparing CSV and XLSX data for an Odoo
19 migration. It helps a data manager govern the source files, capture the
target Odoo schema, and build a validated mapping before any migration work is
considered.

Today, Impodo connects to Odoo with read-only access only. Loading data into
Odoo is not implemented yet; it is the next product capability and will be
delivered shortly.

## The platform

Impodo runs locally on Windows and macOS and opens in the default browser on a
local-only `127.0.0.1` address. Each project has its own local DuckDB database,
which stores project evidence, source inspection results, frozen datasets,
Odoo schema captures, mapping revisions, and validation results.

The platform accepts `.csv` and `.xlsx` source files. It can connect to an
authorised Odoo 19 target. A local Windows instance uses an explicitly
selected `odoo.conf` and fixed read-only metadata operations without an Odoo
API key. A remote instance requires HTTPS and a dedicated read-only API key.
Impodo does not classify targets by an organisation's lifecycle stages.

The browser, source inspection, and read-only Odoo connection work on both
operating systems. The in-browser assistant that discovers and starts a local
Odoo and PostgreSQL stack is currently available on Windows only. On macOS,
start a local Odoo stack separately before connecting to it in Impodo.

## What Impodo does today

### Project setup

- Records the migration context, responsible people, data classification, and
  retention details.
- Adds the governed CSV and XLSX source files and records their hashes.
- Configures and optionally tests the read-only Odoo connection.

### Source discovery and dataset freeze

- Inspects CSV encoding, delimiter, headers, column types, statistics, and
  warnings.
- Inventories XLSX worksheets and named tables, with bounded previews and
  source-file safety checks.
- Lets the user confirm the selected source content and freeze it as named
  datasets. The frozen datasets remain bound to the confirmed source hashes.

### Target schema and governed mapping

- Captures the permitted Odoo models and their fields through read-only
  metadata calls.
- Stores verified model and effective-field snapshots in the project DuckDB
  database, so reopening and mapping do not automatically contact Odoo.
- Records the target business keys and any company or tenant scope fields.
- Maps each frozen dataset to an Odoo model and its writable scalar fields.
- Lets each scalar field use a source column, constant, source fallback, or an
  explicit leave-unset/Odoo-default policy.
- Applies allowlisted trim, whitespace, empty-to-null, casing, locale-aware
  decimal, explicit date-format, boolean, and UTC datetime transformations,
  with bounded raw-to-proposed previews.
- Configures many2one and many2many relationships using governed business
  keys; one2many relationships are handled through the child inverse field.
- Authors hash-bound derived-entity rules that assign deterministic,
  related-entity-owned IDs to reusable values found in denormalized source
  fields, with bounded alias and hierarchy previews.
- Prepares repeated-parent source tables as guided parent and child logical
  datasets, carries every source row into the child dataset, and offers both
  datasets to Mapping with safe inverse-many2one guidance.
- Creates immutable mapping revisions, validates them, and allows submission
  of the exact validated revision after blocking findings are resolved.

Changing a confirmed source, frozen dataset, Odoo schema capture, or governed
business key invalidates the active mapping so it must be validated again.

**Delivery status:** Phase 1 (source discovery), Phase 2A (target schema),
Phase 2B (identities, scope, and relationships), Phase 2C.1 (scalar providers
and transformations), lookup-derived authoring, and mapping-ready parent/child
dataset preparation are implemented. Full-row structural staging, governed lookup
translations, mapping import/export, functional review, and approval remain in
later delivery scope.

## Install and start

Impodo requires Python 3.12 or newer.

### Windows

From PowerShell at the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\impodo.exe
```

The launcher opens a single-use authenticated URL in the default browser. To
stop Impodo, use **Quit Impodo** in the browser or press `Ctrl+C` in the
PowerShell window.

### macOS

From Terminal in a cloned Impodo checkout, create an isolated Python
environment, install Impodo, and choose a local project-data directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
mkdir -p "$HOME/Library/Application Support/Impodo/projects"
export IMPODO_PROJECT_ROOT="$HOME/Library/Application Support/Impodo/projects"
impodo
```

The `impodo` command opens the same local-only browser experience. Keep the
Terminal window open while using Impodo; press `Ctrl+C` or select **Quit
Impodo** in the browser when finished. To keep the chosen project-data
location for future Terminal sessions, add the `export IMPODO_PROJECT_ROOT=...`
line to your shell profile.

Editable installation is the development lane. For use with approved internal
data, promote and install a clean, evidence-producing bundle by following the
[internal development and release runbook](docs/operations/internal-release.md).

## Documentation

For the complete documentation, including the user guide, operating runbooks,
contracts, architecture, and test guidance, start at
[docs/README.md](docs/README.md).
