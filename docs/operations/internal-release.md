# Internal development and release runbook

This runbook keeps Impodo fast to change while preventing an editable checkout
from becoming the normal installation used with company data.

## The two lanes

| Lane | Intended users and data | Required control |
| --- | --- | --- |
| Development | Developers using synthetic or disposable test data | Editable source checkout and an explicit development-mode flag |
| Internal release | Data managers using approved internal data | Clean committed source, locked dependencies, recorded evidence, and a versioned installation |

Development mode is not a weaker production configuration. It is an explicit
exception for short-lived engineering work and must not be used with customer,
confidential, or otherwise controlled data.

## Development lane

Use Python 3.12 and install the editable checkout:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Normal Windows startup stores projects in
`%LOCALAPPDATA%\Impodo\projects` and protects that directory for the current
user, `SYSTEM`, and local Administrators. To deliberately use a repository
directory for disposable development data, both variables are required:

```powershell
$env:IMPODO_DEVELOPMENT_MODE = "1"
$env:IMPODO_PROJECT_ROOT = (Resolve-Path .\var\projects).Path
.\.venv\Scripts\impodo.exe
Remove-Item Env:IMPODO_PROJECT_ROOT
Remove-Item Env:IMPODO_DEVELOPMENT_MODE
```

Without development mode, Windows startup rejects project roots in Git
checkouts, OneDrive, network drives, symbolic links, or junctions.

## Refresh the dependency lock

Run this only when intentionally changing application dependencies:

```powershell
.\scripts\update-internal-lock.ps1
```

The script uses an isolated lock-tool environment under `var`, requires Python
3.12, and regenerates `requirements.windows-py312.lock` with exact versions,
SHA-256 hashes, and binary-only installation policy. Review and commit the
application change and regenerated lock together. The isolated tooling keeps
the lock-generation version independent from the developer virtual
environment.

## Promote a clean commit

Align the environment to the runtime lock, then install the exactly pinned
release tools in a developer or CI virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: `
    --requirement .\requirements.windows-py312.lock
.\.venv\Scripts\python.exe -m pip install -e ".[test,release]"
```

Commit the intended source and confirm that `git status --short` is empty.
Then run:

```powershell
.\.venv\Scripts\python.exe .\scripts\internal_release.py
```

Promotion deliberately refuses a dirty worktree. It builds from a fresh Git
snapshot of the exact commit and rejects wheel contents outside the Impodo
package allowlist, so ignored or stale local build files cannot enter the
release. If successful, it writes one commit-bound directory under
`dist\internal`. The bundle contains:

- the Impodo wheel and the hash-locked dependency file;
- complete automated-test output;
- reviewed secret-scan results;
- a dependency-vulnerability audit;
- a validated CycloneDX SBOM;
- the internal installer; and
- a manifest recording the source commit and SHA-256 hash and size of every
  artifact.

Any failed test, new secret candidate, known dependency vulnerability, invalid
SBOM, missing hash, or non-binary dependency stops promotion.

## Install an accepted bundle

Cybersecurity or Infrastructure must first obtain the bundle through an
approved channel and compare its release manifest or externally recorded hash
with the accepted release record. From inside the bundle, run:

```powershell
.\install-internal-release.ps1
```

The installer verifies every manifest entry before creating a versioned
environment below `%LOCALAPPDATA%\Impodo\app`. It installs only the hashed
locked dependencies and the included wheel, runs `pip check`, and reports the
exact `impodo.exe` path to start. It never silently overwrites an existing
version.

## Current security boundary

The manifest detects changes after a bundle has been obtained from a trusted
source, but it does not authenticate the publisher. Before broad deployment,
Infrastructure must still approve either code signing or an equivalent
application-allowlisting and trusted-distribution process. Until then, this
workflow is appropriate for an explicitly accepted, limited internal pilot;
it is not a generally distributed end-user release.

If installation fails, retain the bundle and console output as evidence. Do
not relax hash checking, suppress a scan, or install an individual dependency
manually. Correct the source or lock and promote a new clean commit.
