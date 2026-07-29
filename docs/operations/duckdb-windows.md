# DuckDB on a Windows laptop

## Who should use this guide

This guide is for an approved support person, developer, or data manager who
has been explicitly asked to install the optional DuckDB command-line tool on
a company-managed Windows laptop.

For normal Impodo use, **do not open DuckDB or run SQL commands yourself**.
Impodo is designed to use DuckDB through its own embedded Python library. The
application, not the data manager, owns its database files, security settings,
and data access. Installing the optional CLI below does not configure Impodo,
does not grant access to customer data, and is not a prerequisite for using a
released Impodo application.

## Before you start

- Use a standard, non-administrator PowerShell window.
- Confirm that company policy permits installing a user-local tool. If in
  doubt, ask IT before continuing.
- The laptop needs internet access to the approved Windows Package Manager
  (`winget`) source. Do not download an installer from an unapproved website.

## Install the optional DuckDB CLI

### Step 1: Open PowerShell

Open **PowerShell** from the Start menu. Do not select **Run as
administrator**.

### Step 2: Confirm that Windows Package Manager is available

Run:

```powershell
winget --version
```

Expected result: a version number is displayed.

If PowerShell says that `winget` is not recognized, stop and contact IT. Do
not substitute a random web download.

### Step 3: Inspect the approved package

Run:

```powershell
winget show --id DuckDB.cli --exact
```

Check that the publisher is **DuckDB** and that the package ID is exactly
`DuckDB.cli`.

### Step 4: Install for your Windows user account

Run:

```powershell
winget install --id DuckDB.cli --exact --scope user --accept-package-agreements --accept-source-agreements
```

This installs a portable DuckDB CLI only for the current Windows user. It does
not require a system-wide installer or administrator rights. Windows Package
Manager verifies the package hash before it installs it.

### Step 5: Start a new PowerShell window

Close the PowerShell window and open a new normal PowerShell window. This is
required because the installer adds DuckDB to your user `PATH` only for new
terminal sessions.

### Step 6: Verify the installation

Run:

```powershell
duckdb --version
```

Expected result: DuckDB prints a version number, for example `v1.x.x`.

### Step 7: Verify that the program opens, then exit

Run:

```powershell
duckdb
```

At the `D` prompt, run:

```sql
SELECT version();
```

Then exit DuckDB:

```text
.quit
```

For Impodo, do not use the CLI to open, edit, inspect, or query any Impodo
database file unless an approved support procedure specifically instructs you
to do so.

## Troubleshooting

### `duckdb` is not recognized

First close PowerShell and open a new window, then repeat Step 6. If it still
does not work, contact IT or the Impodo support owner and provide the output
of:

```powershell
winget list --id DuckDB.cli --exact
```

### Installation is blocked

Stop and provide the exact error to IT. The CLI package supports offline
distribution, but IT must provide any offline package through an approved
company channel and verify its source and checksum.

## Important Impodo boundary

The current Impodo architecture embeds DuckDB through Python and deliberately
does not expose a SQL editor. A future packaged Impodo release should manage
the embedded library itself. This optional CLI is therefore a diagnostic tool,
not an Impodo setup or data-management tool.

## Annex A: `winget` is not installed

Use this annex only when Step 2 reports that `winget` is not recognized.

### Step A1: Open Microsoft Store

Open **Microsoft Store** from the Start menu.

### Step A2: Find the Microsoft package

Search for **App Installer**. Check that the publisher is **Microsoft
Corporation**, then select **Get** or **Install**.

`winget` is delivered as part of App Installer on supported Windows versions.
See Microsoft's [Windows Package Manager installation guidance](https://learn.microsoft.com/en-us/windows/package-manager/winget/).

### Step A3: Verify it is available

Close PowerShell, open a new normal PowerShell window, and run:

```powershell
winget --version
```

When a version is displayed, return to Step 3 of this guide.

### Step A4: Stop if Microsoft Store is blocked

If the Store is unavailable, sign-in is blocked, or App Installer cannot be
installed, stop and contact IT. Do not download `winget` or DuckDB from an
unapproved third-party site.

## Annex B: Python is not installed

This annex is for an approved developer or support person. A data manager who
only uses a released Impodo application does **not** need to install Python.

### Step B1: Check for Python

Run:

```powershell
py --version
```

If `py` is not recognized, also run:

```powershell
python --version
```

If either command displays Python 3.11 or later, Python is already available.
The current Impodo source project requires Python 3.11 or later.

### Step B2: Obtain the approved installer

If neither command works, first obtain company approval. Then use the
[official Python releases for Windows](https://www.python.org/downloads/windows/)
page and choose the current 64-bit Windows installer appropriate for the
company-approved Python version.

Do not use a Python installer from an unapproved mirror. If company policy
requires IT-managed software, send IT the official link instead of attempting
to bypass the policy.

### Step B3: Install and verify Python

Run the approved installer. Keep the installation user-local if that is the
company-approved option. When the installer offers it, enable the option that
makes Python available from the command line.

Close PowerShell, open a new window, and run:

```powershell
py --version
py -m pip --version
```

Both commands must report a version before you continue to Annex C.

## Annex C: Create a Python virtual environment

Use a virtual environment only for approved development or support work on
the Impodo source checkout. It isolates that project's Python packages from
the shared Python installation. Python's standard-library
[virtual-environment documentation](https://docs.python.org/3/library/venv.html)
explains the underlying tool.

### Step C1: Open PowerShell in the project folder

Run this from the root of the Impodo source checkout, where `pyproject.toml`
is located:

```powershell
Set-Location C:\path\to\dev-impodo
```

Replace the example path with the actual local project folder.

### Step C2: Create the environment

Run:

```powershell
py -m venv .venv
```

This creates a `.venv` folder inside the project. It is local working state
and must not be committed to Git.

### Step C3: Verify the environment without activating it

Run:

```powershell
.\.venv\Scripts\python.exe -m pip --version
```

Using the environment's Python executable directly avoids PowerShell script
execution-policy issues that can affect activation scripts.

### Step C4: Install the project dependencies

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Only install `duckdb` into this environment when an approved Impodo code or
packaging change declares it as a project dependency. Record that dependency
in the project's package configuration; do not rely on an undocumented
machine-local installation.

### Step C5: Stop if environment creation is blocked

If `py -m venv .venv` or pip reports a permissions error, stop and provide
the exact error to IT or the Impodo support owner. Do not work around company
security controls by copying package files or using an unapproved installer.
