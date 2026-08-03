# Windows workstation requirements for Impodo

## Purpose

This document tells what a Windows laptop must provide so that an authorised user can install and run Impodo successfully.

Impodo runs as a local Python application and opens its user
interface in the laptop's default browser. It stores its working data on that
laptop and can connect to an Odoo 19 DEV or TEST environment.

## Required workstation baseline

IT must provide all of the following:

| Requirement | What IT must provide |
| --- | --- |
| Local storage | Writable local `%LOCALAPPDATA%` and `%TEMP%` directories for the user. The Impodo project directory must be on a local fixed drive. |
| Browser | A company-managed default browser that permits cookies and JavaScript on `http://127.0.0.1` and allows the user to download generated files. |
| Loopback access | Permission for Impodo to bind to a temporary port on `127.0.0.1` and for the browser to connect to it. This is same-laptop traffic, not a LAN service. |
| Application execution | Endpoint allowlisting rules that permit the approved Python runtime, the installed `impodo.exe`, and the Python executables inside the versioned Impodo installation. |
| PowerShell | Permission for IT to run the approved `install-internal-release.ps1` script. The data manager does not need PowerShell for normal use after a shortcut is created. |
| Credential storage | Windows Credential Manager must be available if a remote Odoo API key will be saved between Impodo sessions. |

No inbound firewall rule or fixed TCP port is required. Impodo selects a free
loopback port every time it starts and does not listen on the laptop's LAN
address.

If PowerShell execution policy or application control blocks unsigned scripts,
IT must allow the exact approved installer or require the release team to
provide it through the organisation's signing and packaging process. The data
manager must not be asked to bypass those controls.

## Software that must be installed

### 1. Company-managed 64-bit CPython 3.12

The current Windows release requires **64-bit CPython 3.12.x**. The Python
launcher must be able to select it with:

```powershell
py -3.12 --version
py -3.12 -c "import platform; print(platform.architecture()[0])"
```

The commands must report Python 3.12 and `64bit`. Installing a newer Python
version without 3.12 does not satisfy the current internal installer, because
the installer explicitly calls `py -3.12`.

The current installer verifies the Python 3.12 version but does not itself
verify 64-bit architecture. IT must therefore perform the architecture check
above before installation.

Python must include the standard `venv` module and pip. IT can verify both
without changing the machine by running:

```powershell
py -3.12 -m venv --help
py -3.12 -m pip --version
```

### 2. The approved Impodo internal release

IT must obtain the accepted Impodo Windows release bundle through the
organisation's approved software-distribution channel. The current bundle
format contains the Impodo wheel, a hash-locked dependency file, a release
manifest, and `install-internal-release.ps1`.

From the extracted release-bundle directory, IT installs it with:

```powershell
.\install-internal-release.ps1
```

The script verifies the bundle manifest, creates a versioned installation
under `%LOCALAPPDATA%\Impodo\app`, installs the locked dependencies, runs
`pip check`, and prints the full path of the installed `impodo.exe` launcher.
IT should create a Start menu or desktop shortcut to that exact launcher so
the data manager does not need to use PowerShell for normal operation.

The current installer retrieves third-party Python wheels while it runs.
Therefore, the installation account must be able to reach an approved Python
package source through the corporate network and proxy, with its TLS
certificate trusted by Python. If public package access is prohibited, the
release team must first provide an internally hosted package source or a new
release bundle containing the required wheels; the current bundle is not a
complete offline installer.

Package-source access is an installation requirement only. During normal use,
Impodo does not need general internet access; it needs only the connectivity
required for the selected Odoo DEV/TEST environment.

### 3. Python dependencies installed by the Impodo installer

IT does not install these components individually. The release installer
installs the exact locked versions, including:

- DuckDB's embedded Python library;
- FastAPI and Uvicorn for the laptop-local browser application;
- OpenPyXL for Excel workbooks;
- the Windows keyring integration used with Credential Manager; and
- the remaining packages declared by the Impodo release.

The complete package and version list is recorded in the
[Windows Python 3.12 dependency lock](../../requirements.windows-py312.lock).

Do not replace the locked dependency installation with individually selected
package versions.

## Software that is not required

A normal data-manager laptop does **not** need:

- the standalone DuckDB command-line tool;
- Git or a cloned Impodo source repository;
- Node.js;
- a separate web server such as IIS, Apache, or nginx;
- a DuckDB server;
- direct PostgreSQL access or database administration tools; or
- Odoo and PostgreSQL installed locally when Impodo uses a remote Odoo
  DEV/TEST environment.

Developers using an editable source checkout have different requirements.
Those are documented in the
[internal development and release runbook](internal-release.md) and must not
be used as the standard data-manager installation procedure.

## Local folders and permissions

For normal use, Impodo creates and uses:

```text
%LOCALAPPDATA%\Impodo\app\<release-id>    installed application
%LOCALAPPDATA%\Impodo\projects            project databases and source evidence
%TEMP%                                    temporary installation and processing files
```

The user must be able to create and modify content in these locations. At
startup, Impodo protects the project directory so that only the current user,
`SYSTEM`, and local Administrators have access.

The project directory must not be redirected to or placed inside:

- OneDrive or another synchronised folder;
- a network share;
- a removable drive;
- a Git checkout; or
- a symbolic link or junction.

Impodo refuses normal startup for removable or network drives, configured
OneDrive roots, Git checkouts, links, and junctions. IT must ensure that no
other synchronisation or folder-redirection product encompasses
`%LOCALAPPDATA%\Impodo\projects`, leave it on a writable local fixed drive, and
allow Impodo to set and verify its protected Windows access-control list.

IT must reserve enough free disk space for the installed application, the
source files copied into each project, the project DuckDB database, and the
generated evidence. The project does not yet publish a release-qualified
minimum free-space figure; capacity must be based on the expected number and
size of migration files and monitored during the pilot.

## Odoo connectivity requirements

Choose one of the following operating modes for the laptop.

### Remote Odoo DEV/TEST

For the normal remote mode, IT and the Odoo administrator must provide:

- outbound HTTPS access from the laptop to the approved Odoo 19 DEV/TEST URL,
  including any required corporate LAN or VPN route;
- a TLS certificate chain trusted by the installed Python 3.12 runtime;
- the exact Odoo base URL and database-routing name;
- a dedicated read-only Odoo API key for the user or service identity; and
- Odoo ACLs, record rules, company scope, and model/field access that permit
  the required read-only metadata and record queries.

Plain HTTP is not accepted for a remote Odoo server. Impodo does not require
an Odoo master password, PostgreSQL password, SSH access, or an inbound
connection from Odoo.

If the organisation uses an authenticated proxy, TLS inspection, or endpoint
web filtering, IT must verify the Impodo Python process—not only the
browser—can reach the Odoo URL successfully.

### Odoo running on the same laptop

Local mode is optional and is a separate workstation setup. If it is required,
IT must additionally provide a working Odoo 19 development/test installation,
its compatible PostgreSQL service, and a readable `odoo.conf`. The Odoo HTTP
endpoint and PostgreSQL listener must bind to loopback. Impodo can inspect and,
for a compatible local workspace, start that stack, but it does not install
Odoo or PostgreSQL.

A local Odoo installation is not needed when the approved target is a remote
DEV/TEST environment.

## Source-file access

The user must be able to select approved `.csv` and `.xlsx` files from a local
location accessible to their Windows account. Endpoint protection must be
able to scan those files without preventing Impodo from reading them.

Do not use email attachments, removable media, network shares, or synchronised
folders as the active Impodo project store. Files received through an approved
channel should first be placed in the organisation's approved local working
location; Impodo then copies registered evidence into its protected project
directory.

## IT installation verification

The laptop is ready for the user only when all of these checks pass:

- [ ] The Python checks report Python 3.12 and `64bit`.
- [ ] The accepted internal release installs without hash, dependency, proxy,
      certificate, permission, EDR, or application-control errors.
- [ ] IT has created a usable shortcut to the installed `impodo.exe`.
- [ ] Starting the shortcut opens the managed browser at an address beginning
      with `http://127.0.0.1:`.
- [ ] Impodo can create `%LOCALAPPDATA%\Impodo\projects` and apply its protected
      access-control list.
- [ ] A small approved CSV or XLSX file can be added to a test project.
- [ ] The intended Odoo 19 DEV/TEST connection passes Impodo's connection test.
- [ ] Quitting Impodo stops the local application, and starting the shortcut
      again reopens the existing test project.

After these checks, give the user the shortcut, the approved Odoo connection
details, and the
[local-browser user guide](local-browser-user-guide.md).

## Current qualification gaps

The repository currently establishes the runtime and operational requirements
above, but it does not yet define a release-qualified Windows-version matrix,
browser-version matrix, minimum CPU/RAM specification, or minimum free-disk
figure. Those values must be measured on the intended corporate Windows build
and expected migration size before IT publishes them as guaranteed minimums.
