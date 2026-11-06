---
audience: developer
kind: plan
status: approved
---

# Weekly source releases and continuous integration

## Decision and intended reader

The maintainer approved this process on 6 November 2026. Contributors integrate
small, verified changes throughout the week. The maintainer publishes a weekly
source release from one verified commit on `main`.

This replaces the original first-release procedure and its `0.1.0` examples.
The existing release baseline is `v26.10.4`. Application versions follow
`YY.MM.W`, as described below.

The process applies to human contributors, coding agents, and the maintainer
preparing a release. It does not configure branch protection, change workflow
triggers, authorize a merge, or publish a release merely because this plan is
approved. Those actions require their own authorized implementation.

## Current state and temporary Actions pause

As observed on 6 November 2026:

- The existing release tag is `v26.10.4`, at commit
  `af2f694d13c4bc42a1dc710c02416097848fbf9d`.
- The committed project version is still `26.10.4`.
- The maintainer has paused **Python quality**, **Linux server quality**, and
  **Windows release quality** in GitHub. Their files, tests, and run history
  remain present.
- GitHub reported an account billing lock for the last attempted hosted run.
  Its jobs did not execute their checks.
- Separate worktrees, pull requests, and required checks are the approved
  development process. Repository-side enforcement and CI cost improvements
  still need implementation and verification.

Keep these workflows disabled until the maintainer explicitly asks to resume
them. Do not enable, dispatch, or rerun them, or change triggers to bypass the
pause. Read-only status checks and local verification remain allowed. Preparing
this plan does not authorize a billing change or a new paid runner.

While Actions are paused, contributors may prepare changes, run local checks,
and push authorized task branches. Keep pull requests unmerged and releases
as drafts when required hosted evidence is unavailable. A local pass or a
billing failure does not establish a passing Linux, macOS, or Windows matrix.

## Give each change an owner and an isolated checkout

Each coherent change has one owner, one short-lived task branch, and its own
worktree. An example branch is `fix/payment-term-matching`. Other agents can
review the change without editing its files concurrently.

Create the worktree from the intended committed base and give it a separate
Python environment. Confirm that imports and spawned workers use that checkout.
Carry local instructions and tooling into the worktree without tracking them;
verify that instruction, skill, environment, and tool paths remain ignored.

Use explicit file scope when staging. A contributor must not collect every
pending edit in a shared checkout into a release snapshot. Before delivery,
compare the committed or staged tree with the candidate that passed checks.
Later edits belong to a later candidate.

### Branch naming convention

Use `<type>/<slug>`. Only these three types are allowed for development branches:

| Type | Purpose | Examples |
| --- | --- | --- |
| `feat/` | Add or improve functionality, including supporting documentation and refactoring. | `feat/shared-practice-data` |
| `fix/` | Correct a defect, regression, or inaccurate documentation. | `fix/payment-term-matching` |
| `release/` | Prepare or stabilize a release, or maintain the release process. | `release/26.11.1`, `release/process` |

Use a short, descriptive, lowercase slug with words separated by hyphens.
Release-version slugs may contain periods. Do not add nested path components,
spaces, personal names, or tool-specific prefixes. Validate the selected name
with `git check-ref-format --branch <name>` before creating it. `main` remains
the integration branch, rather than a development-branch type.

Classify a documentation or refactoring task by its purpose. A correction uses
`fix/`, an improvement uses `feat/`, and release-process work uses `release/`.
Use `release/<version>` for a concrete release candidate; the branch is not a
shared integration stream for that week's unrelated features.

An owner adopting this convention must rename their own nonconforming branch
before further work. Preserve its worktree and pending changes. Do not rename
another agent's active branch, delete a published branch, or rewrite remote
history without an authorized, coordinated migration.

### Challenge proposals that create material risks

Contributors and agents must challenge a proposed approach when it conflicts
with accepted contracts or established practice, or creates a material
reliability, security, maintainability, or cost risk. Explain the concrete
consequence, identify the supporting evidence or uncertainty, and recommend an
alternative before proceeding with the affected decision.

Distinguish requirements from local conventions. Branch prefixes describe the
purpose of a change; safe integration still depends on isolation, review, and
verification. Follow the maintainer's informed decision within the remaining
guardrails.

### Transition existing shared work safely

The shared checkout already contains concurrent work. Each owner must identify
and inspect their own files before transferring a bounded change into an
isolated worktree. Preserve the shared copies until the transfer is reconciled
and ownership is clear.

Do not switch the dirty shared checkout to another branch, reset it, apply a
repository-wide stash, or delete another contributor's changes. If ownership
overlaps, reconcile the affected lines before editing or staging them. Starting
a release branch does not resolve a shared-worktree race.

## Integrate verified changes throughout the week

1. Implement one coherent responsibility on its task branch. Run focused
   tests for that responsibility and its affected callers. Cover Odoo 19 and
   Odoo 20, local and remote access, and Recipe paths when affected.
2. Run the complete repository gate on the exact candidate before committing,
   pushing, or handing it off. Preserve all diagnostics and report failed or
   omitted checks.
3. When authorized, push the task branch and open a pull request into
   `main`. Explain the resulting behavior, validation, and compatibility risks.
4. Before merging, require the complete portable behavior matrix, repository
   checks, default performance checks, and applicable Linux server and Windows
   checks to pass for the proposed merge. Resolve review feedback and conflicts.
5. Merge ready changes regularly after the required evidence passes. Verify
   the resulting `main` commit before selecting it for a release.

Ordinary development must not commit or push directly to `main`. Keep `main`
releasable through small reviewed integrations. A permanent `develop` branch
or a shared weekly version branch is not the normal integration path.

Use a short stabilization branch only when a concrete release needs one.
Restrict it to release fixes, verify them through pull requests, and bring the
accepted fixes back into `main`. Do not start a parallel stream of unrelated
feature work there. The release identity is the tag and commit.

Creating a branch does not authorize a remote push, pull request, merge, tag,
or publication. Carry out only the external actions included in the user's
request. Preparing release text authorizes a draft.

## Verify the exact candidate and control CI cost

The local complete repository gate is:

```powershell
uv run --frozen --group quality python scripts/check_repository_quality.py
```

Use the checkout's pinned runner and an environment belonging to that checkout.
The gate includes static quality, architecture, and documentation checks. It
does not replace focused behavior tests or the full portable behavioral suite.

For shared-contract refactors and repairs of repeated behavioral CI failures,
run all four portable shards from `.github/workflows/python-quality.yml` on
one frozen candidate before delivery. Keep the full operating-system matrix
and applicable platform checks as merge and release requirements. A passing
subset does not establish that the complete candidate is ready.

When hosted workflows are explicitly resumed, improve their execution order:

- Run the fast repository checks before starting the expensive behavior matrix.
- Cancel superseded runs for the same workflow and pull request. Keep unrelated
  pull requests and the selected release verification independent.
- Collect each distinct failure across shards and platforms before choosing a
  repair. Inspect job annotations when no test step ran.
- Attribute every result to its commit or candidate tree. Recheck after a
  source, dependency, version, or merge change invalidates that evidence.
- Investigate slow tests and excessive repeated work without weakening behavior,
  evidence checks, platform coverage, or required diagnostics.

These are proposed workflow changes, not a claim that the current YAML already
implements them. The Actions pause remains in force during their preparation.

## Weekly release window

Use Friday at the end of the working day as the regular release decision
window. The maintainer selects the cutoff and the exact candidate. No scheduled
job automatically merges branches or publishes a release.

The next proposed weekly slots are:

| Decision window | Proposed version | Proposed tag |
| --- | --- | --- |
| 6 November 2026 | `26.11.1` | `v26.11.1` |
| 13 November 2026 | `26.11.2` | `v26.11.2` |

These are release opportunities. If required checks are unavailable or fail,
keep the release in draft and record the blocker. If there is no meaningful
change since the previous release, skip the slot. Do not create a new tag on
identical source merely to satisfy the calendar.

After eight successful source releases, review whether a monthly cadence would
better match development. Continue weekly until the maintainer approves that
change. Urgent security and data-integrity fixes need not wait for Friday.

## Calendar version and source identity

Use `YY.MM.W`: the two-digit year, the two-digit month, and the
maintainer-selected weekly slot within that month. For example, `26.11.1`
identifies the first planned November 2026 weekly release. A skipped slot does
not require renumbering later slots.

The release-preparation change updates `pyproject.toml` and synchronizes the
project entry in `uv.lock` with the pinned uv runner. The annotated tag is
exactly `v` followed by the project version. The Release title uses that same
version. Do not substitute the obsolete `0.1.0` examples or infer compatibility
from a calendar number.

The maintainer chooses a distinct version for an urgent corrective release.
Agents must not invent a suffix or move an existing tag to reuse an identity.
Record the selected version before preparing its source and notes.

Application release numbering is independent of storage schema and portable
evidence contract versions. Explain incompatible project layouts, supported
upgrades, and any required operator action in the notes. A new application
version does not itself prove that an older project can be opened safely.

## Source-only release contents

A public release consists of an immutable annotated Git tag, its exact source
commit, reviewed release notes, and GitHub's automatic source archives.
Maintainers upload no custom files.

Do not add an installer, wheel, custom archive, container, checksum, SBOM,
attestation, automatic updater, deployment, or long-term-support promise to
this workflow without a separate distribution decision. Accepted internal
bundles remain a separate organizational process.

## Prepare and publish one release

1. Identify the previous reachable release tag and the intended included
   changes. Finish their reviewed feature integrations before the cutoff.
2. Prepare the version, synchronized lockfile, and notes on a
   `release/<version>` branch. Describe implemented behavior only. Keep unfinished
   work and proposals outside the release claims.
3. Run focused checks and the complete repository gate. Obtain the complete
   required hosted evidence before merging the release-preparation pull request.
4. Verify the exact resulting `main` commit. Record its full SHA, version,
   comparison range, local results, and hosted CI links. A test of an earlier
   feature head does not establish that this merged commit passed.
5. If any required check fails, is skipped without an applicable exclusion, or
   cannot run, keep the release in draft. A maintainer waiver must name the
   exact failed or omitted guardrail and the single delivery it covers.
6. When separately authorized, create and push the annotated `v<version>` tag on
   that verified SHA. Confirm that the remote tag resolves to the intended
   commit. Never move a tag once it has become visible.
7. When authorized, create a draft GitHub Release for the existing tag. Check
   the title, version, source commit, notes, comparison, CI evidence, and absence
   of custom assets. Publish only within the maintainer's authorization.
8. Verify the public Release and tagged-source installation in a clean isolated
   directory. Do not interrupt the maintainer's running Impodo session or write
   to a live Odoo target as part of this verification.

The release checkout must contain the exact reviewed candidate. Unrelated dirty
work in another worktree can remain in progress; it is not part of the release.

## Release-note contract

Write for an evaluator or data manager who needs to understand the practical
changes and compatibility boundaries. Use only sections that help that reader:

```markdown
# Impodo <version>

## Release summary

Explain why this release matters.

## Highlights

- Describe the most important implemented user outcomes.

## Reliability and safety

- Describe meaningful correctness, evidence, recovery, or bounded-work changes.

## Compatibility and upgrade information

State supported environments, incompatible layouts, and upgrade requirements.

## Install from source

Link the tagged source and its installation instructions.

## Known limitations

State material missing behavior and verification limits.

## Source

Link the tag, full commit, comparison range, and exact-commit CI results.
```

Until the final candidate is verified, label the notes as provisional and name
the committed range they describe. Exclude uncommitted work from shipped claims.
Record omitted validation honestly. Generated commit lists are an inventory,
not the public explanation.

## Failure and correction policy

A failed required gate blocks delivery. Do not classify an unrelated failure
as acceptable debt or treat a disabled workflow as a passing check. Correct the
source through its owned task branch and verify a new candidate.

After publication, never replace the source behind a published tag. Publish a
new verified corrective version. For a security, authorization, evidence,
journaling, or reconciliation defect, warn affected users and prepare the
correction promptly. Changing release notes to add a warning is permitted;
retargeting the released source is not.

## Remaining enforcement work and acceptance criteria

The process is adopted when agents read the shared instructions, own isolated
feature worktrees, stage bounded changes, and preserve the Actions pause.
Repository enforcement still needs explicit implementation and verification:

- Protect `main` with required pull requests, resolved review conversations,
  passing required checks, and protection against force pushes and deletion.
- Apply the CI execution order and concurrency controls described above after
  the maintainer authorizes resuming hosted checks.
- Protect published version tags and verify the available immutable-release
  settings before changing them.
- Keep any future tag workflow limited to verifying the selected source and
  preparing a draft with minimum permissions. Human authorization remains
  required for publication.

A release is complete only when its project version, lockfile, tag, notes,
source SHA, and verification evidence agree, no custom asset was uploaded, and
the public source instructions work for the released revision.

## References

- [GitHub flow](https://docs.github.com/en/get-started/using-github/github-flow).
- [Git worktrees](https://git-scm.com/docs/git-worktree).
- [Protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
- [Workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
- [Managing releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository?tool=cli).
