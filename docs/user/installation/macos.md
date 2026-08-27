# Install Impodo on macOS

Impodo is a local application that opens in your normal browser. It does not
install Odoo, PostgreSQL, or a database server.

Use this GitHub-checkout route for development or evaluation with synthetic or
disposable data. Do not use an unreviewed checkout with confidential pilot
data. An accepted internal macOS release bundle is not currently available.

## Before you start

You need an approved Git installation and Python 3.12 or newer. This guide
uses Python 3.14 because it is available on the Mac used to maintain Impodo.
If your organization provides a different approved Python 3.12-or-newer
command, use that command consistently in every step below.

Open **Terminal** and confirm that Git and Python 3.14 are available:

```bash
git --version
python3.14 --version
```

The first command must report a Git version. The second command must report a
Python version beginning with `Python 3.14`. If either command is unavailable,
stop and install the approved tool or ask your IT team for help. Do not use
`sudo`, install Impodo packages globally, or bypass your organization's
software controls.

## 1. Download Impodo

Create a local Applications folder if you do not already have one, then open
it in Terminal:

```bash
mkdir -p "$HOME/Applications"
cd "$HOME/Applications"
```

Keep the checkout outside iCloud Drive, Dropbox, network drives, and other
synchronized folders. If you already have an Impodo checkout, open it instead
of downloading a second copy:

```bash
cd "$HOME/Applications/dev-impodo"
```

Otherwise, download the repository and open the downloaded folder:

```bash
git clone https://github.com/fderyckel/dev-impodo.git
cd "$HOME/Applications/dev-impodo"
```

Git may open a browser and ask you to sign in with an authorized GitHub
account. If Git reports `Repository not found`, ask the repository owner to
confirm that your account has access.

Confirm that Terminal is in the Impodo folder:

```bash
test -f pyproject.toml && echo "Impodo project file found"
test -d src/impodo && echo "Impodo application folder found"
```

Both confirmation messages must appear. If either one does not appear, return
to the folder that Git downloaded before continuing.

## 2. Create Impodo's private Python environment

Run:

```bash
python3.14 -m venv .venv
test -x .venv/bin/python && echo "Impodo Python environment found"
```

The confirmation message means that `.venv` contains this checkout's private
Python environment. It keeps Impodo's packages separate from other Mac
applications. Do not copy this folder between computers or add it to Git.

## 3. Install Impodo and its required libraries

Run the installation command and wait for Terminal to return to the prompt:

```bash
.venv/bin/python -m pip install -e .
```

This can take several minutes while Python downloads and installs Impodo's
required libraries. This repository declares those libraries in
`pyproject.toml`; there is no separate `requirements.txt` to install for a
GitHub checkout.

Verify that Python installed Impodo and that every installed requirement is
consistent:

```bash
.venv/bin/python -m pip check
.venv/bin/python -m pip show impodo
test -x .venv/bin/impodo && echo "Impodo launcher found"
```

Continue only when:

- `pip check` reports `No broken requirements found`.
- `pip show` displays `Name: impodo`.
- The launcher confirmation message appears.

These checks confirm that the private environment contains Impodo, its
declared libraries do not have missing or conflicting requirements, and the
browser launcher was created. You do not need to activate `.venv` because the
commands in this guide use its Python and launcher directly.

## 4. Choose where Impodo stores your data projects

Impodo keeps each data project, its source evidence, and its workspace results
in a local project-data folder. Create an owner-private folder outside the Git
checkout:

```bash
mkdir -p "$HOME/Library/Application Support/Impodo/projects"
chmod 700 "$HOME/Library/Application Support/Impodo/projects"
```

The folder is separate from the application checkout. Updating or replacing
the checkout does not change the data projects stored there. Do not move,
rename, or delete active project folders outside Impodo.

## 5. Start Impodo

From the `dev-impodo` folder, run:

```bash
IMPODO_PROJECT_ROOT="$HOME/Library/Application Support/Impodo/projects" .venv/bin/impodo
```

Impodo opens a single-use authenticated address in your default browser. Keep
the Terminal window open while you use the application.

## Confirm the first start

The browser address should begin with `http://127.0.0.1:` and show the
**Projects** page. This means Impodo is running only on your Mac. Select **New
project** when you are ready to begin.

Impodo creates and uses the project-data folder that you selected in the
previous step. The application does not automatically start a local Odoo or
PostgreSQL stack on macOS. Start a local stack separately before connecting to
it in Impodo, or use an authorized remote Odoo connection.

![The current empty Data projects page after a fresh authenticated start, with New project as the next action.](../../images/user/01-project-list.png)

To stop Impodo, select **Quit Impodo** in the browser or press `Control+C` in
Terminal. On later starts, return to the same `dev-impodo` folder and run only
the command in **Start Impodo**. You do not need to repeat the installation.

If the browser does not open, keep the Terminal window open and give its exact
error to the person supporting the installation. Do not disable antivirus,
broaden folder permissions, or add a public firewall exception.
