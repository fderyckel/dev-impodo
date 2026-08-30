# Install Impodo on Windows

Impodo is a local application that opens in your normal browser. It does not
install Odoo, PostgreSQL, or a database server.

## Choose one installation method

Use only one of these methods:

- **GitHub checkout:** use this for development or evaluation with synthetic or
  disposable data. Follow **Method A** below.
- **Accepted Impodo release:** use this for an approved internal pilot with
  confidential data. Your organization's Impodo release owner must first give
  you the complete accepted release folder. Follow **Method B** below.

Do not use an unreviewed GitHub checkout with confidential pilot data.

## Install Python with Winget

Both installation methods require approved 64-bit Python 3.12. Use these steps
when your organization permits installation through the Windows Package
Manager (`winget`).

Open the Windows Start menu, type **PowerShell**, and open PowerShell normally.
Do not select **Run as administrator**. Confirm that Winget is available:

```powershell
winget --version
```

Install Python 3.12 for your Windows account:

```powershell
winget install --id Python.Python.3.12 --exact --scope user --source winget --accept-package-agreements --accept-source-agreements
```

Close PowerShell after installation and open a new PowerShell window. Confirm
Python:

```powershell
py -3.12 --version
```

The result must begin with `Python 3.12`. If `winget` is unavailable or the
installation is blocked, you can use the project-local `uv` alternative in
Method A when your organization approves the required downloads. Otherwise,
ask IT for the approved 64-bit Python 3.12-or-newer package. Do not disable
endpoint protection or use an unapproved package source.

## Method A — Install from GitHub

This is an editable development or evaluation installation. Do not use an
unreviewed Git checkout with confidential pilot data; use the accepted release
installation described below instead.

### 1. Install Git

In the new PowerShell window, install Git for your Windows account:

```powershell
winget install --id Git.Git --exact --scope user --source winget --accept-package-agreements --accept-source-agreements
```

Close PowerShell, open it again, and confirm Git:

```powershell
git --version
```

The result must begin with `git version`. If it does not, stop and ask IT for
the approved Git for Windows package.

### 2. Download Impodo

First create a local `Applications` folder if needed and open it in
PowerShell:

```powershell
$impodoApplications = Join-Path $env:USERPROFILE "Applications"
$null = New-Item -ItemType Directory -Path $impodoApplications -Force
Set-Location -LiteralPath $impodoApplications
```

If a `dev-impodo` folder already exists there, do not clone it again. Open that
folder with `Set-Location -LiteralPath .\dev-impodo` and continue at the two
`Test-Path` checks below.

Otherwise, download Impodo:

```powershell
git clone https://github.com/fderyckel/dev-impodo.git
```

Wait until Git finishes and the PowerShell prompt returns. Only then open the
downloaded folder:

```powershell
Set-Location -LiteralPath .\dev-impodo
```

The folder is kept outside OneDrive, SharePoint, network drives, and other
synchronized locations. Git may open a browser and ask you to sign in with an
authorized GitHub account. If Git reports `Repository not found`, stop and ask
the repository owner to confirm that your GitHub account has access.

Confirm that PowerShell is in the downloaded Impodo folder:

```powershell
Test-Path -LiteralPath .\pyproject.toml
Test-Path -LiteralPath .\src\impodo
```

Both commands must return `True`.

### 3. Create Impodo's private Python environment

Run:

```powershell
py -3.12 -m venv .venv
Test-Path -LiteralPath .\.venv\Scripts\python.exe
```

The last command must return `True`. The `.venv` folder keeps Impodo's Python
packages separate from other applications. Do not copy this folder between
computers or add it to Git.

### 4. Install Impodo and its requirements

Run the installation command and wait for it to finish:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

This may take several minutes. PowerShell may print many package names while it
works. When the prompt returns, check that the output contains no line beginning
with `ERROR:`. Then verify the installation:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pip show impodo
Test-Path -LiteralPath .\.venv\Scripts\impodo.exe
```

This repository declares its application requirements in `pyproject.toml`.
There is no separate `requirements.txt` to install for an editable checkout.
The `requirements.windows-py312.lock` file is used by the accepted-release
process. `pip install -e .` reads `pyproject.toml`, downloads the declared
requirements from the configured package source, and creates
`.\.venv\Scripts\impodo.exe`.

Continue only when:

- `pip check` reports `No broken requirements found`;
- `pip show` displays `Name: impodo`; and
- `Test-Path` returns `True`.

You do not need to activate `.venv`. Every command in this guide uses the
correct executable inside it directly.

### Alternative to steps 3 and 4: use a project-local Python with uv

Use this route only for a GitHub checkout when `winget` or App Installer is
unavailable. Before you continue, your organization must approve downloads
from the official Astral and Python package sources. This route does not
change Windows, install a system Python, or bypass your organization's
endpoint controls.

Run these commands from the `dev-impodo` folder. They download the portable
`uv` tool into this checkout, then use it to download Python 3.14, create
Impodo's private `.venv` folder, and install the exact package versions in
`uv.lock`:

```powershell
$impodoTools = Join-Path $PWD ".tools"
$impodoUvArchive = Join-Path $impodoTools "uv.zip"
$null = New-Item -ItemType Directory -Path $impodoTools -Force
Invoke-WebRequest -Uri "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $impodoUvArchive
Expand-Archive -LiteralPath $impodoUvArchive -DestinationPath $impodoTools -Force

$env:UV_CACHE_DIR = Join-Path $PWD ".uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD ".python"
& ".\.tools\uv.exe" sync --locked --python 3.14
```

The first run can take several minutes. The `.tools`, `.python`, `.uv-cache`,
and `.venv` folders belong only to this checkout. This repository ignores
them, so do not add them to Git or copy them to another computer.

When the command finishes, verify the private Python environment and the
locked installation:

```powershell
.\.venv\Scripts\python.exe --version
& ".\.tools\uv.exe" sync --locked --check --python 3.14
Test-Path -LiteralPath .\.venv\Scripts\impodo.exe
```

Continue only when the first command begins with `Python 3.14`, the second
command reports that it would make no changes, and the last command returns
`True`. Continue at **5. Start Impodo**.

### 5. Start Impodo

Run:

```powershell
.\.venv\Scripts\impodo.exe
```

Keep the PowerShell window open while using Impodo. On later starts, return to
the same `dev-impodo` folder and run only the launcher command. You do not need
to repeat the installation.

## Method B — Install an accepted Impodo release

Use this route only after your organization's Impodo release owner has given
you the complete, accepted release folder. If it was delivered as a ZIP file,
extract the entire ZIP into an approved local folder first. Keep every supplied
file together and unchanged.

The correct folder is the extracted folder that directly contains at least:

```text
release-manifest.json
install-internal-release.ps1
requirements.windows-py312.lock
impodo-<version>.whl
```

The wheel's actual filename includes its version and build information. A
normal Git checkout is not this folder and does not contain these generated
release files.

1. In File Explorer, open the folder containing
   `release-manifest.json` and `install-internal-release.ps1`.
2. Right-click an empty area in that folder and select **Open in Terminal**.
3. Confirm that PowerShell is in the correct folder:

   ```powershell
   Test-Path -LiteralPath .\release-manifest.json
   Test-Path -LiteralPath .\install-internal-release.ps1
   ```

   Both commands must return `True`. If either returns `False`, stop and locate
   the complete accepted release folder.

4. Run the supplied installer:

   ```powershell
   powershell.exe -ExecutionPolicy Bypass -File .\install-internal-release.ps1
   ```

   `Bypass` applies only to this new PowerShell process and the supplied
   installer command. It does not permanently change the computer's execution
   policy.

5. Wait for every verification and installation step to complete.
6. When installation succeeds, PowerShell prints `Start it with:` followed by
   the complete launcher path. The path has this shape:

   ```text
   %LOCALAPPDATA%\Impodo\app\<release-id>\Scripts\impodo.exe
   ```

7. Copy only the path printed after `Start it with:`. In the same PowerShell
   window, type `&`, a space, and a double quote; paste the path; add the closing
   double quote; then press Enter. The command will look like this:

   ```powershell
   & "<the complete launcher path printed by the installer>"
   ```

   Impodo then starts the accepted release.

Do not run the release installer from a Git checkout. A source tree does not
contain the generated manifest, wheel, and evidence required by the installer.

The current installer downloads locked third-party wheels from the approved
package source. It is not a fully offline or digitally signed enterprise
installer.

## Confirm the first start

The browser address should begin with `http://127.0.0.1:` and show the
**Projects** page. This means the installation succeeded. Select **New
project** when you are ready to begin.

Impodo stores project data under `%LOCALAPPDATA%\Impodo\projects` by default.
Do not move, rename, or delete active project folders outside Impodo.

![The current empty Data projects page after a fresh authenticated start, with New project as the next action.](../../images/user/01-project-list.png)

If the browser does not open, keep the PowerShell window open and give its
exact error to the person supporting the installation. Do not disable
antivirus, broaden folder permissions, or add a public firewall exception.

For development checkouts and detailed Windows troubleshooting, see the
[developer Windows setup guide](../../developer/setup/windows.md). The bundle
promotion and acceptance process is documented in the
[internal release runbook](../../developer/runbooks/internal-release.md).
