# Local worktree containment

This runbook is for developers and automation that need a disposable Git
worktree while working on Impodo. The repository contains source code, tests,
and documentation that must remain versioned. A local worktree is a separate,
temporary checkout and must not become repository content.

## Create local worktrees in one contained location

Create a local worktree below `.local-worktrees/`. Git ignores that directory,
so the parent repository cannot accidentally stage the worktree as a Git link.

```powershell
New-Item -ItemType Directory -Force .local-worktrees | Out-Null
git worktree add --detach .local-worktrees/<purpose> <commit-or-branch>
```

You can also use the existing ignored `.tmp/` directory for short-lived,
tool-created worktrees. Do not create a single-letter worktree such as `r/`,
`t/`, `u/`, `v/`, `w/`, or `x/` at the repository root. Those names previously
leaked into Git as Git links and are explicitly ignored as a safeguard.

## Before staging changes

Run `git status --short` from the repository root. Stage only the source,
test, documentation, or configuration files that belong to the intended
change. Never use a broad `git add .` when a local worktree is present.

The root `tests/` directory is not a local worktree. It contains the Impodo
test suite and must remain tracked so that every checkout can verify the
application.

## Remove a finished worktree

Use Git to remove a finished worktree so that its directory and Git metadata
are removed together.

```powershell
git worktree remove .local-worktrees/<purpose>
```

If a worktree contains work you need, commit it or copy the intended changes
to a normal branch before removal.
