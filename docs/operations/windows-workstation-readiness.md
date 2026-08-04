# Windows workstation readiness

## Scope

This checklist is for IT teams preparing a Windows laptop for the current
Impodo internal pilot. Impodo runs locally in the user's browser and stores
project data under the user's profile. The accepted installation route uses a
versioned internal bundle and Python 3.12.

The current bundle is an internal release mechanism, not a signed,
enterprise-wide installer and not a fully offline package.

## Required workstation baseline

| Area | Requirement |
| --- | --- |
| Operating system | Supported 64-bit Windows workstation |
| Browser | Current managed Edge, Chrome, or equivalent Chromium browser |
| Python | Approved 64-bit CPython 3.12 available through the `py` launcher |
| PowerShell | Local `.ps1` execution allowed under the organization's policy |
| Local storage | Write access to `%LOCALAPPDATA%\Impodo` and a usable user temp directory |
| Loopback | Browser access to the Impodo localhost listener |
| Network | HTTPS access to the approved Python package source during installation and to approved remote Odoo targets when used |
| Credentials | Approved credential or secret-management mechanism; no shared API keys |
| Endpoint controls | Application allowlisting and antivirus rules must permit the accepted Python runtime and bundle scripts |
| Device security | Organization-standard encryption, patching, screen lock, and endpoint protection |

Source files and generated evidence may contain personal or commercially
sensitive data. Provision enough encrypted local storage for source copies,
snapshots, reports, and versioned installations.

## Verify Python

Run:

```powershell
py -3.12 -c "import struct,sys; print(sys.version); print(struct.calcsize('P') * 8)"
```

The command must report Python 3.12 and `64`. Do not substitute another minor
version for the accepted internal bundle.

## Install an accepted bundle

Place the immutable release bundle in an approved local directory, keep every
file together, and run:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\install-internal-release.ps1
```

The script verifies the release manifest and artifact hashes, creates a
versioned environment below `%LOCALAPPDATA%\Impodo\app`, installs dependencies
from the checked-in hashed lock, installs the Impodo wheel, and runs
`pip check`.

It retrieves locked dependency wheels from the configured package source, so
network and certificate policy must allow that source. Do not manually edit
the lock or remove hashes to work around a failed installation.

Start Impodo with the exact launcher path printed by the installer. A later
bundle installs beside the earlier version; IT can remove an obsolete version
through the organization's normal software-removal process after evidence and
project retention have been reviewed.

## Odoo access

For a remote target, provide:

- the approved HTTPS base URL and database name;
- a dedicated, least-privilege read-only Odoo 19 account;
- an API key delivered through the approved secret channel;
- network, proxy, DNS, and TLS access to the target.

Never place the API key in a project file, profile, screenshot, support ticket,
or shared command history.

For the optional local development stack, follow the
[local Odoo runbook](local-odoo.md). Local and remote identify connection
location; they are not DEV/TEST lifecycle labels.

## Source and output access

The user needs read access to approved CSV/XLSX sources and write access to
the selected Impodo workspace. Do not use a synchronized or shared folder
unless its retention, access, and conflict behavior are approved for the data.

The browser treats registered sources as immutable evidence. Corrections are
made in a new source file and registered as a new revision, not by silently
editing the registered copy.

## Optional DuckDB support tool

The DuckDB CLI is not required for normal Impodo use. Impodo manages its own
DuckDB files, and support staff must not open an active project database with
the CLI.

If an approved diagnostic procedure explicitly requires the CLI:

```powershell
winget show --id DuckDB.cli --exact
winget install --id DuckDB.cli --exact --scope user --accept-package-agreements --accept-source-agreements
```

Open a new PowerShell session and verify:

```powershell
duckdb --version
```

Query only an approved copy or disposable diagnostic database. Never edit an
Impodo-managed project database.

## Readiness checklist

- [ ] Managed browser can reach the local Impodo URL.
- [ ] Approved 64-bit Python 3.12 is available through `py -3.12`.
- [ ] User-local application and temp directories are writable.
- [ ] PowerShell can run the accepted installer.
- [ ] Package-source access works for locked wheel downloads.
- [ ] The release manifest and all listed artifacts remain together.
- [ ] Odoo access uses a dedicated read-only identity.
- [ ] Source and output locations meet data-handling requirements.
- [ ] The installed launcher starts Impodo successfully.

Release production and troubleshooting details are in the
[internal release runbook](internal-release.md).
