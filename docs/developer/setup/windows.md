# Set up an editable Impodo checkout on Windows

## Purpose

This developer guide starts at the point where the Impodo source code has been
downloaded from GitHub. It explains every required step to install and start
an editable checkout of the local browser application on a Windows laptop.

The GitHub route is an **editable development or evaluation installation**.
Use synthetic or disposable data unless the organization has reviewed and
accepted the exact source revision and installation process. For confidential
pilot data, the preferred route remains an accepted internal release bundle.
See [Install an accepted Impodo release on Windows](../../user/installation/windows.md).

Impodo does not install Odoo, PostgreSQL, or a database server. It runs as a
local Python application, opens the user's normal browser, and stores projects
under the Windows user profile.

## What is required

| Item | Requirement |
| --- | --- |
| Windows | A supported 64-bit Windows laptop managed according to organizational policy |
| Python | Approved 64-bit CPython 3.12 with the Windows `py` launcher, `pip`, and `venv` |
| Browser | A current managed Edge, Chrome, or equivalent Chromium browser |
| PowerShell | A normal user PowerShell window; administrator mode is not normally required |
| Storage | A local, writable source-code folder plus writable `%LOCALAPPDATA%` and `%TEMP%` |
| Network during installation | HTTPS access to the organization's approved Python package source |
| Loopback | Browser access to a random local address on `127.0.0.1` |
| Endpoint controls | Antivirus and application controls that permit the approved Python runtime and Impodo |
| Device controls | Required encryption, patching, screen lock, firewall, and endpoint protection |

The following are **not** required for the GitHub ZIP route:

- Git is not required unless the repository was cloned and will be updated
  with Git.
- Virtual-environment activation is not required.
- Node.js is not required.
- The DuckDB command-line tool is not required.
- PostgreSQL tools are not required.
- A local Odoo installation is not required when the project uses a remote
  Odoo target.
- A fixed inbound firewall port is not required.
- Administrator rights are not required unless the organization's Python or
  endpoint policy requires them.

## Step 1 — Extract the GitHub download

If GitHub supplied a ZIP file:

1. In File Explorer, right-click the ZIP and select **Extract all**.
2. Choose a short local path owned by the user, for example:

   ```text
   C:\Users\<WindowsUser>\Applications\dev-impodo
   ```

3. Do not run Impodo from inside the compressed ZIP.
4. Avoid OneDrive, SharePoint, network drives, shared folders, and other
   synchronized locations.

The extracted Impodo folder must directly contain at least:

```text
README.md
pyproject.toml
requirements.windows-py312.lock
src
docs
```

Git is not required when the source was downloaded as a ZIP. If the repository
was cloned instead, use the clone's root folder for the same steps below.

## Step 2 — Install approved 64-bit Python 3.12

Obtain the 64-bit Python 3.12 installer from the organization's software
catalogue or another approved source. During installation, include:

- Include the Python launcher for Windows.
- Include `pip`.
- Include `venv`.

Adding `python.exe` to `PATH` is optional because this guide uses the Windows
`py` launcher explicitly.

### Optional — Add `python.exe` to the user `PATH`

Adding Python to `PATH` lets PowerShell run `python` and `pip` without their
full paths. Impodo does not require this, but it can be useful for other Python
work.

The recommended method is through the approved Python installer:

1. On the installer's first page, select **Add python.exe to PATH**.
2. Complete the Python installation.
3. Close every open PowerShell window and open a new one. Existing windows do
   not automatically receive the changed `PATH`.

If Python 3.12 is already installed, run the same approved installer again,
choose **Modify**, continue to **Advanced Options**, select **Add Python to
environment variables**, and complete the modification.

If the organization's installer does not expose that option, add the folders
through Windows user settings:

1. Run this command to find the approved Python 3.12 executable:

   ```powershell
   py -3.12 -c "import sys; print(sys.executable)"
   ```

2. Copy the folder containing `python.exe`. A typical per-user result is:

   ```text
   C:\Users\<WindowsUser>\AppData\Local\Programs\Python\Python312
   ```

3. Open the Windows Start menu, search for **Edit environment variables for
   your account**, and open it.
4. Under **User variables**, select `Path`, then select **Edit**.
5. Add the Python folder from step 2.
6. Add its `Scripts` subfolder as a separate entry, for example:

   ```text
   C:\Users\<WindowsUser>\AppData\Local\Programs\Python\Python312\Scripts
   ```

7. Confirm every dialog with **OK**.
8. Close every PowerShell window and open a new one.

Add these entries to the current user's `Path`, not the system-wide `Path`,
unless IT specifically requires a machine-wide installation. Do not add
Impodo's `.venv\Scripts` folder permanently to either `Path`.

Confirm the result in the new PowerShell window:

```powershell
Get-Command python | Select-Object -ExpandProperty Source
python --version
python -c "import struct,sys; print(sys.executable); print(struct.calcsize('P') * 8)"
where.exe python
```

The checks must show:

- `Get-Command python` must resolve to the approved Python installation,
  normally a path ending in `Python312\python.exe`.
- `python --version` must begin with `Python 3.12`.
- The architecture check must print the same approved executable followed by
  `64`.
- `where.exe python` may list more than one executable, but the approved
  `Python312\python.exe` must appear before any
  `Microsoft\WindowsApps\python.exe` entry.

If only `Microsoft\WindowsApps\python.exe` appears, or running `python` opens
the Microsoft Store, the real Python installation is not correctly available
through `PATH`. Recheck the user `Path` entries. If the Store alias takes
precedence, search the Start menu for **Manage app execution aliases** and turn
off only the App Installer aliases for `python.exe` and `python3.exe`. Then open
a new PowerShell window and repeat the four checks.

Close any existing PowerShell windows after installing Python. Open a new
PowerShell window and run:

```powershell
py -3.12 -c "import struct,sys; print(sys.version); print(struct.calcsize('P') * 8)"
```

Continue only when the first line begins with `3.12` and the second line is:

```text
64
```

If `py -3.12` is not found, stop here. Installing packages into a different
Python version does not satisfy the supported Windows setup.

## Step 3 — Open PowerShell in the Impodo folder

In File Explorer, open the extracted folder, right-click an empty area, and
select **Open in Terminal**. Alternatively, open PowerShell and enter the
folder explicitly:

```powershell
Set-Location -LiteralPath "C:\Users\<WindowsUser>\Applications\dev-impodo"
```

Replace the example with the actual extracted path. Confirm that PowerShell is
in the correct folder:

```powershell
Get-Location
Test-Path -LiteralPath .\pyproject.toml
Test-Path -LiteralPath .\src\impodo
```

Both `Test-Path` commands must return `True`.

All remaining GitHub-installation commands in this guide must be run from this
folder.

## Step 4 — Create Impodo's private Python environment

Create a virtual environment inside the downloaded folder:

```powershell
py -3.12 -m venv .venv
```

Verify that its Python executable exists:

```powershell
Test-Path -LiteralPath .\.venv\Scripts\python.exe
```

The result must be `True`.

The `.venv` folder isolates Impodo's Python packages from other applications
on the laptop. It must not be copied between laptops or committed to Git.

There is no need to activate it. Every command below names its Python or
launcher executable directly.

## Step 5 — Install Impodo and its dependencies

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

This command:

- It reads `pyproject.toml` from the current folder.
- It downloads the required Python packages from the configured package
  source.
- It installs the packages only inside `.venv`.
- It creates the Windows launcher `.\.venv\Scripts\impodo.exe`.

The `-e` option means editable installation. The installed launcher uses the
source code in this downloaded folder after Impodo is restarted. It does not
turn the checkout into an accepted or immutable release.

Do not use `sudo`, do not install the dependencies globally, and do not remove
dependency constraints to work around an installation failure.

## Step 6 — Verify the installation

Run all three checks:

```powershell
.\.venv\Scripts\python.exe -m pip show impodo
.\.venv\Scripts\python.exe -m pip check
Test-Path -LiteralPath .\.venv\Scripts\impodo.exe
```

The expected results are:

- `pip show` displays the Impodo package and the downloaded source folder as
  its editable project location.
- `pip check` reports `No broken requirements found`.
- `Test-Path` returns `True`.

Do not continue if `pip check` reports a dependency conflict.

## Step 7 — Start Impodo for the first time

Run:

```powershell
.\.venv\Scripts\impodo.exe
```

Expected behavior:

1. Impodo prepares the protected project directory.
2. It binds to a random local port on `127.0.0.1`.
3. It creates a single-use authenticated launch URL.
4. The default browser opens the local Impodo page.
5. The PowerShell window remains open while Impodo is running.

Normal Windows project storage is:

```text
%LOCALAPPDATA%\Impodo\projects
```

Confirm the resolved location with:

```powershell
Join-Path $env:LOCALAPPDATA "Impodo\projects"
```

Impodo creates and verifies the protected directory itself. Do not configure
project storage inside the GitHub checkout, Downloads, OneDrive, a shared
folder, or a network drive.

If PowerShell prints a warning that **Impodo development mode does not enforce
the internal-data storage policy**, stop the application and use only
synthetic data until `IMPODO_DEVELOPMENT_MODE` has been removed from the user
environment.

## Step 8 — Confirm the first launch

Before using project data, verify:

- The browser address begins with `http://127.0.0.1:`.
- The port number can change on every start.
- The Impodo project page loads without an authentication error.
- The PowerShell window shows no project-root or dependency error.
- `%LOCALAPPDATA%\Impodo\projects` exists after the first launch.

Use a sanitized test project for the first end-to-end check. A successful
browser launch proves the local application is running; it does not yet prove
remote Odoo permissions, source-data acceptance, or production readiness.

## Step 9 — Stop and restart Impodo

To stop Impodo safely, use **Quit Impodo** in the browser. If the browser is no
longer available, focus the PowerShell window and press `Ctrl+C` once.

To start it again later:

1. Open PowerShell in the same downloaded folder.
2. Run only:

   ```powershell
   .\.venv\Scripts\impodo.exe
   ```

The installation command does not need to be repeated for an unchanged
checkout.

## Step 10 — Prepare Odoo access when required

Impodo can start without Odoo. A complete migration workflow additionally
needs an approved Odoo 19 target.

For a remote target, obtain through the approved process:

- Obtain the HTTPS Odoo base URL.
- Obtain the database name.
- Provision a dedicated, least-privilege Odoo identity.
- Provision the required read or disposable-target load API key.
- Confirm approved DNS, proxy, VPN, routing, and TLS certificate access.

Never place an API key in a project file, mapping, screenshot, support ticket,
shared command, or Git checkout. Impodo stores governed credentials in memory
or the Windows credential vault.

For an optional disposable local Odoo 19 laboratory, follow the
[local Odoo runbook](../runbooks/local-odoo.md). Impodo does not install Odoo or
PostgreSQL as part of the application installation.

## Step 11 — Update a GitHub installation

Stop Impodo before updating it.

### If the repository was cloned with Git

Confirm that the checkout has no work that would be overwritten, update it,
and rerun installation so changed dependencies or entry points are applied:

```powershell
git status --short
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip check
```

Do not discard local changes merely to make `git pull` succeed.

### If the source came from a GitHub ZIP

Download and extract the new revision into a new local folder. Create a new
`.venv` there and repeat Steps 3–7. Do not copy the old `.venv` into the new
folder.

Existing projects remain under `%LOCALAPPDATA%\Impodo\projects`; replacing a
source folder does not authorize deleting or moving that governed project
data. Review release compatibility and retention requirements before changing
the application revision used for an active migration.

## Accepted internal bundles

The editable setup above is not the installation path for an approved
confidential-data pilot. Data managers should follow
[Install an accepted Impodo release on Windows](../../user/installation/windows.md).
Developers producing or promoting that bundle should follow the
[internal release runbook](../runbooks/internal-release.md).

## Troubleshooting

### `py` or Python 3.12 is not found

Close PowerShell, install the approved 64-bit Python 3.12 runtime with the
Windows launcher, and open a new PowerShell window. Re-run the verification in
Step 2.

### Creating `.venv` reports `Access is denied`

Confirm that the extracted folder and Windows temporary directory are writable
by the current user and are not controlled by a sync client. Endpoint security
may need to allow the approved Python runtime. Do not disable antivirus or
change broad folder permissions without IT approval.

### Package installation cannot reach the package source

Preserve the complete error and ask IT to verify the approved package index,
proxy, TLS inspection, and certificate trust. Do not add an unapproved index,
disable certificate verification, or remove hashes from an internal bundle.

### `impodo.exe` was not created

Return to the folder containing `pyproject.toml` and rerun:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip check
```

Resolve any reported installation error before launching.

### The browser does not open

Keep the PowerShell error output. Press `Ctrl+C`, confirm that Windows has an
approved default browser and that endpoint controls permit local loopback,
then start Impodo again. The authenticated launch URL is single-use; do not
bookmark or share it.

### Impodo refuses the project-data root

Normal Windows use must keep project data in
`%LOCALAPPDATA%\Impodo\projects`. Remove unsupported
`IMPODO_PROJECT_ROOT` or `IMPODO_DEVELOPMENT_MODE` overrides from the user
environment and start Impodo again. Do not use real customer data in
development mode.

### Windows Firewall asks about Python

Impodo listens only on `127.0.0.1` and requires no inbound LAN firewall rule.
Follow organizational policy; do not create a broad public or private network
exception merely to run the local browser application.

## Final readiness checklist

- [ ] The GitHub ZIP is fully extracted to a short, local, user-writable path.
- [ ] `py -3.12` reports approved 64-bit Python 3.12.
- [ ] `pyproject.toml` and `src\impodo` are present in the working folder.
- [ ] `.venv\Scripts\python.exe` exists.
- [ ] `pip install -e .` completes successfully.
- [ ] `pip check` reports no broken requirements.
- [ ] `.venv\Scripts\impodo.exe` exists and starts successfully.
- [ ] The managed browser opens the authenticated `127.0.0.1` page.
- [ ] Project data is stored under `%LOCALAPPDATA%\Impodo\projects`.
- [ ] No development-mode warning appears before confidential data is used.
- [ ] Remote Odoo, local Odoo, and API credentials are provisioned only when
      required for the selected workflow.
- [ ] Confidential pilot use follows an accepted revision and installation
      route rather than an unreviewed GitHub checkout.
