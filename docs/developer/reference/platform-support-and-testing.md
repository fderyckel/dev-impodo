---
audience: developer
document_type: reference
status: current
---

# Platform support and testing

## Supported development and runtime profiles

Impodo development and local use must remain portable across Windows and
macOS. The shared remote-server profile targets Linux. Application and domain
behavior must not change merely because the host operating system changes.

Platform-specific code is acceptable only at a real adapter boundary, such as
the Windows PowerShell release installer or Linux filesystem permission bits.
Callers should depend on the portable contract rather than branching throughout
the application.

## Test ownership

Tests below `tests/architecture/` are part of the portable repository gate.
They must run with the same expectations on Linux, macOS, and Windows. They
must not skip or branch on `os.name` or `sys.platform`.

Tests that execute a host facility belong below:

- `tests/platform/windows/` for Windows PowerShell, Windows packaging, and
  other Windows-owned behavior;
- `tests/platform/macos/` for behavior genuinely owned by macOS; and
- `tests/platform/linux/` for Linux permissions, service management, container
  hosts, and other Linux-owned behavior.

Do not classify a test as platform-specific merely because it once failed on
one operating system. First remove path assumptions, shell assumptions, and
environment leakage. Move the test only when the behavior being verified is
itself host-specific.

## Continuous integration lanes

`.github/workflows/python-quality.yml` runs the complete portable repository
gate on `ubuntu-latest`, `macos-latest`, and `windows-latest`. Its final
`quality` job preserves the required `Python quality / quality` check name and
passes only when the complete matrix passes.

Host-owned suites run separately:

- `.github/workflows/windows-release-quality.yml` runs the Windows installer
  tests when the installer or its release inputs change.
- `.github/workflows/linux-server-quality.yml` runs Linux permission and server
  integration tests when the deployment or server composition changes.

This separation keeps the common gate portable without discarding valuable
platform evidence.

## Temporary files and local tooling

Tests create disposable artifacts with the operating system's temporary-file
APIs. They must not assume that the ignored repository-local `.tmp/` directory
exists. A test that needs a specific filesystem capability belongs in the
corresponding platform suite.

`.agents/` remains ignored local tooling state. Tracked documentation, tests,
and CI workflows must never require it. Repository documentation may describe
the governed process, but its links and manifest entries resolve only to
tracked repository content.

## Verification

Run the complete portable gate from the repository root:

```powershell
uv run --frozen --group quality python scripts/check_repository_quality.py
```

On the owning host, also run the affected platform suite with `unittest`
discovery rooted at its `tests/platform/<platform>/` directory.

## Related documentation

- [Python code quality](python-code-quality.md)
- [Code organization](../../architecture/code-organization.md)
- [Linux server implementation plan](../../plans/linux-server-postgresql-duckdb-implementation.md)
