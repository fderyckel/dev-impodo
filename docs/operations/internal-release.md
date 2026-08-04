# Internal development and release runbook

## Scope

This runbook separates an editable development checkout from an accepted
Windows internal-release bundle. The release process is for a limited pilot;
it does not claim code signing, offline installation, centralized deployment,
or enterprise support readiness.

## Development checkout

Use 64-bit Python 3.12 and a repository-local virtual environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip check
```

Start the browser application with:

```powershell
.\.venv\Scripts\impodo.exe
```

Editable installs are for development only. They neither update dependency
versions nor prove that the checked-in release lock is valid.

## Dependency lock

[`requirements.windows-py312.lock`](../../requirements.windows-py312.lock) is
the authority for Windows/Python 3.12 internal releases. It is generated with
hashes and binary-only resolution. Multiple hashes on a requirement cover
compatible wheel artifacts; they are not duplicate versions.

Refresh it only after an intentional dependency change:

```powershell
.\scripts\update-internal-lock.ps1
```

Review both the requested dependency change and the complete lock diff. Do
not hand-edit hashes, upgrade a transitive package in isolation, or use an
unlocked environment as release evidence.

## Build an immutable bundle

Promotion requires a clean Git worktree and the exact pinned release tools.
From an environment installed with the project development dependencies, run:

```powershell
.\.venv\Scripts\python.exe .\scripts\internal_release.py
```

The promotion gate validates the lock, materializes the committed revision,
runs the required test and security evidence steps, builds and validates the
wheel, and publishes one immutable directory below `dist\internal`.

A release bundle includes:

- the Impodo wheel and hashed dependency lock;
- test, secret-scan, dependency-audit, and SBOM evidence;
- the installer;
- a manifest containing artifact sizes and SHA-256 hashes.

The gate refuses a dirty worktree, inconsistent tooling, invalid lock, failed
evidence step, or an existing destination. Inspect any retained work directory
before retrying; do not delete evidence reflexively.

## Accept and install a bundle

Release acceptance is an organizational decision made after reviewing the
bundle and its evidence. Transfer the entire accepted directory without
adding or changing files.

On the prepared workstation, run from inside the bundle:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\install-internal-release.ps1
```

The installer verifies every listed artifact before creating a versioned
environment under `%LOCALAPPDATA%\Impodo\app`. Dependencies are installed
with `--require-hashes` and `--only-binary=:all:`, then the wheel is installed
without resolving new dependencies and `pip check` verifies consistency.

The bundle does not contain all third-party wheels, so installation still
needs the approved Python package source. The manifest detects accidental or
unauthorized file changes after publication, but it is not a digital
signature and does not by itself establish publisher identity.

## Release checklist

- [ ] Intended dependency changes are reflected in `pyproject.toml` and the lock.
- [ ] Lock refresh and review used Python 3.12.
- [ ] Worktree is clean and the intended revision is checked out.
- [ ] Promotion finishes without bypassing a gate.
- [ ] Evidence and manifest are reviewed before acceptance.
- [ ] The complete, unchanged bundle is transferred.
- [ ] Installer reports success and prints a versioned launcher.
- [ ] Workstation requirements in the
      [readiness guide](windows-workstation-readiness.md) are satisfied.
