# Local Phase A browser

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
5. Configure an HTTPS Odoo DEV or TEST target.
6. Optionally test the connection with a dedicated read-only API key.
7. Review the completeness list and select **Register project**.

The API key can stay in process memory or be saved in Windows Credential
Manager. It never enters the project database or registration manifest.

## Stop

Use **Quit Impodo** in the browser footer. Closing the tab alone does not stop
the local Python process. `Ctrl+C` in the launching PowerShell window also
stops it.

## Current boundary

The browser implements Phase A project registration. The **Continue to Phase
B** action is deliberately disabled until source inventory and preview are
implemented. There is no Odoo write capability and no Production option.

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
