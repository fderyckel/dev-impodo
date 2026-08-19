# Install an accepted Impodo release on Windows

Use this guide for an approved internal pilot on a managed 64-bit Windows
laptop. It assumes that your organization has provided an accepted Impodo
release bundle. A GitHub checkout is a development installation and is not the
approved route for confidential pilot data.

Impodo is a local application that opens in your normal browser. It does not
install Odoo, PostgreSQL, or a database server.

## Before you start

You need:

- an accepted Impodo release bundle kept unchanged in an approved local folder;
- approved 64-bit Python 3.12 with the Windows `py` launcher; and
- permission to install from the approved Python package source used by your
  organization.

The bundle contains a release manifest, the Impodo package, locked dependency
requirements, release evidence, and `install-internal-release.ps1`.

## Install the release

1. Open PowerShell in the accepted release-bundle folder.
2. Run:

   ```powershell
   powershell.exe -ExecutionPolicy Bypass -File .\install-internal-release.ps1
   ```

3. Wait for every verification and installation step to complete.
4. Copy the exact versioned launcher path printed by the installer. It has this
   shape:

   ```text
   %LOCALAPPDATA%\Impodo\app\<release-id>\Scripts\impodo.exe
   ```

5. Run that launcher to start the accepted release.

Do not run the installer script from a GitHub source checkout. A source tree
does not contain the generated manifest, wheel, and evidence required by the
release installer.

The current installer downloads locked third-party wheels from the approved
package source. It is not a fully offline or digitally signed enterprise
installer.

## Confirm the first start

The browser should open an authenticated page on `127.0.0.1`. Impodo stores
project data under `%LOCALAPPDATA%\Impodo\projects` by default. Do not move,
rename, or delete active project folders outside Impodo.

![Current Impodo Recipes page shown after a successful authenticated start.](../../images/user/01-project-list.png)

If the browser does not open, keep the PowerShell window open and give its
exact error to the person supporting the installation. Do not disable
antivirus, broaden folder permissions, or add a public firewall exception.

For development checkouts and detailed Windows troubleshooting, see the
[developer Windows setup guide](../../developer/setup/windows.md). The bundle
promotion and acceptance process is documented in the
[internal release runbook](../../developer/runbooks/internal-release.md).
