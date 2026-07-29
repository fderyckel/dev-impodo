# Local project browser

## Install

From a normal PowerShell window in the repository:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Python 3.11 or newer is required. The editable install provides the `impodo`
launcher.

## Start

```powershell
.\.venv\Scripts\impodo.exe
```

Impodo binds an operating-system-selected port on `127.0.0.1` and opens a
single-use authenticated launch URL in the default browser. It does not listen
on the LAN.

The normal Windows project location is:

```text
%LOCALAPPDATA%\Impodo\projects
```

For an isolated development or test store only:

```powershell
New-Item -ItemType Directory -Force .\var\projects | Out-Null
$env:IMPODO_PROJECT_ROOT = (Resolve-Path .\var\projects).Path
.\.venv\Scripts\impodo.exe
Remove-Item Env:IMPODO_PROJECT_ROOT
```

`var/` is excluded from Git. Do not use an examples, fixtures, output, shared,
email-synchronised, or Git-tracked directory for customer data.

## Register a project

1. Select **New project**.
2. Enter the source and export details.
3. Record the data manager, functional owner, classification, and retention.
4. Add one or more `.csv` or `.xlsx` source files.
5. Choose **Local Odoo** or **Remote / on-premises Odoo**, then configure a
   DEV or TEST target.
6. Optionally test the connection with a dedicated read-only API key.
7. Review the completeness list and select **Register project**.

The API key can stay in process memory or be saved in Windows Credential
Manager. It never enters the project database or registration manifest. A
stored key is bound to the exact project, connection mode, URL, and database;
changing the target requires the appropriate key for the new destination.

## Inspect registered sources

After registration:

1. Select **Continue to Phase B**.
2. Select **Inspect source files**.
3. Review detected CSV encoding/delimiter or XLSX worksheets and named tables.
4. Review the candidate header, bounded preview, column types, statistics, and
   file warnings.

Inspection recalculates each stored file's size and SHA-256 hash before parsing
it. Results are stored in the project DuckDB database and can be regenerated
without modifying the source file or the registered project.

### Local Odoo mode

Use this for an Odoo 19 instance running on the same computer:

- connection mode: `Local Odoo`;
- environment: `DEV`;
- base URL: `http://127.0.0.1:8069` for the standard local port;
- database: the exact local database name, for example `odoo19_dev`;
- credential: an API key for a dedicated least-privilege internal Odoo user.

HTTP is accepted only for literal IPv4 or IPv6 loopback addresses. `localhost`,
LAN addresses, credentials in the URL, URL fragments, and extra paths are
rejected. Start PostgreSQL and Odoo before selecting **Save and test
connection**.

To create the credential in Odoo 19, sign in as the dedicated internal user,
open that user's preferences, find **API Keys**, and select **Add API Key**.
Copy the displayed key when it is generated. Do not use the Odoo master
password, PostgreSQL password, or the user's interactive password.

### Remote / on-premises mode

Use this for a server-hosted Odoo DEV or TEST instance:

- connection mode: `Remote / on-premises Odoo`;
- base URL: the approved HTTPS URL;
- database: the database-routing name supplied by the Odoo administrator;
- credential: an expiring API key for a dedicated least-privilege user.

The TLS certificate must be trusted by the Python runtime on the workstation.
Plain HTTP and loopback destinations are rejected in this mode. Production is
not an available environment.

## Stop

Use **Quit Impodo** in the browser footer. Closing the tab alone does not stop
the local Python process. `Ctrl+C` in the launching PowerShell window also
stops it.

## Current boundary

The browser implements Phase A project registration and the first Phase B
source-inventory and preview slice. Interactive encoding, delimiter, worksheet,
and header confirmation are not yet implemented; neither are mapping or
staging. There is no Odoo write capability and no Production option.

## Verify

Run all automated tests with a writable temporary directory:

```powershell
New-Item -ItemType Directory -Force .\.tmp | Out-Null
$env:TEMP = (Resolve-Path .\.tmp).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Remove-Item Env:TEMP
Remove-Item Env:TMP
```
