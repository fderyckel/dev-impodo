# Impodo

Impodo is a local browser platform for preparing CSV and XLSX data for an Odoo
19 migration. It helps a data manager govern the source files, capture the
target Odoo schema, and build a validated mapping before any migration work is
considered.

It connects to Odoo with read-only access only. Impodo does not create, update,
delete, or import Odoo records.

## The platform

Impodo runs on the user's Windows computer and opens in the default browser on
a local-only `127.0.0.1` address. Each project has its own local DuckDB
database, which stores project evidence, source inspection results, frozen
datasets, Odoo schema captures, mapping revisions, and validation results.

The platform accepts `.csv` and `.xlsx` source files. It can connect to an
Odoo 19 DEV or TEST environment with a dedicated read-only API key. A local
Odoo instance is supported; a remote Odoo instance must use HTTPS. Production
is not an available target.

## What Impodo does today

### Phase A: Register a migration project

- Records the migration context, responsible people, data classification, and
  retention details.
- Adds the governed CSV and XLSX source files and records their hashes.
- Configures and optionally tests the read-only Odoo connection.

### Phase 1: Inspect and freeze source data

- Inspects CSV encoding, delimiter, headers, column types, statistics, and
  warnings.
- Inventories XLSX worksheets and named tables, with bounded previews and
  source-file safety checks.
- Lets the user confirm the selected source content and freeze it as named
  datasets. The frozen datasets remain bound to the confirmed source hashes.

### Phase 2B: Govern the Odoo schema and validate mappings

- Captures the permitted Odoo models and their fields through read-only
  metadata calls.
- Records the target business keys and any company or tenant scope fields.
- Maps each frozen dataset to an Odoo model and its writable scalar fields.
- Configures many2one and many2many relationships using governed business
  keys; one2many relationships are handled through the child inverse field.
- Creates immutable mapping revisions, validates them, and allows submission
  of the exact validated revision after blocking findings are resolved.

Changing a confirmed source, frozen dataset, Odoo schema capture, or governed
business key invalidates the active mapping so it must be validated again.

## Install and start

Impodo requires Python 3.11 or newer. From PowerShell at the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\impodo.exe
```

The launcher opens a single-use authenticated URL in the default browser. To
stop Impodo, use **Quit Impodo** in the browser or press `Ctrl+C` in the
PowerShell window.

## Documentation

For the complete documentation, including the user guide, operating runbooks,
contracts, architecture, and test guidance, start at
[docs/README.md](docs/README.md).
