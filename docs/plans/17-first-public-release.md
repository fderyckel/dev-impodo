---
audience: developer
kind: plan
status: approved
---

# Main integration and weekly source releases

## Current decision

On 7 November 2026 the maintainer replaced the task-branch and pull-request
process adopted on 6 November with direct integration on `main`. Contributors
work in the original checkout and give each task clear file ownership.
A delivery collects inspected changes into one fixed, locally verified
snapshot and pushes that exact commit to `main` when authorized.

The earlier branch naming and mandatory pull-request procedure are superseded.
Existing worktrees remain available until their work has been reconciled.
The shared ignored `AGENTS.md` carries the current instructions for agents.

## Preserve concurrent work and the running application

Inspect existing edits before modifying a shared file. Reconcile overlapping
responsibilities and preserve later changes from other agents. Avoid switching
a dirty checkout, resetting shared files, or stashing unrelated work together.

Before delivery, save the included files and patches and record the cutoff.
An isolated detached checkout with its own Python environment may be used to
test that snapshot while other work continues. Confirm that imports and
spawned workers use its source. Keep local instructions, skills, tool links,
environments, and verification artifacts ignored.

Stage explicit inspected paths. Compare the committed tree with the exact
candidate that passed checks. Changes made after the cutoff belong to the
next delivery. Preserve the original worktree copies until their transfer
and integration are accounted for.

Updating the running application's source can trigger its build-change guard.
Preserve the maintainer's Impodo session and obtain approval before stopping
or restarting it. A verified snapshot can be pushed from the detached checkout
while the running local checkout waits for an approved synchronization.

## Verify and deliver to main

1. Review the included changes and reconcile conflicts against current `main`.
2. Run focused tests for every changed responsibility and affected caller.
   Cover Odoo 19 and 20 and the local, remote, authoring, and Recipe paths
   affected by the change.
3. Run the complete repository gate on the fixed candidate.
4. Record the candidate tree and successful local checks. Report failed,
   skipped, and unavailable checks separately.
5. When authorized, commit and push that exact candidate directly to `main`.
   Use a normal fast-forward push. Reconcile a changed remote and recheck
   the resulting candidate before retrying. Preserve shared history.
6. Reconcile the original checkout only when its later edits and running
   application can be preserved.

The complete repository gate uses the checkout's pinned toolchain.

```powershell
uv run --frozen --group quality python scripts/check_repository_quality.py
```

The gate includes Python quality, architecture tests, documentation checks,
and code-documentation checks. It supplements focused behavioral tests.
A failure blocks delivery until corrected or explicitly waived by the
maintainer for the named guardrail and single delivery.

## Keep GitHub Actions paused and manual

The maintainer paused **Python quality**, **Linux server quality**, and
**Windows release quality** on 6 November 2026. Keep those workflows disabled.
Their tracked YAML uses only `workflow_dispatch`. The maintainer chooses each
workflow, source revision, and run. Pushing code does not authorize dispatching,
rerunning, enabling workflows, changing billing, or provisioning paid runners.

GitHub's generated Dependency Graph uses a separate repository security
setting. It remains automatic and cannot be disabled through the workflow API
with the current connection. An administrator must review that setting and
its effect on dependency alerts and dependency review.

Record hosted checks as unexecuted while they are paused. Local integration
is separate from qualification for a public release. A skipped or unavailable
hosted check is never a passing result.

## Weekly release identity

Use Friday at the end of the working day as the normal release decision
window. The maintainer selects the cutoff and the verified `main` commit.
The current unpublished draft is `26.11.1`, following `v26.10.4` at
`af2f694d13c4bc42a1dc710c02416097848fbf9d`.

Application versions follow `YY.MM.W`, using the two-digit year, two-digit
month, and maintainer-selected weekly slot within that month. The first
November slot is `26.11.1`. The next proposed slot is `26.11.2` on 13 November
2026. A skipped slot does not require renumbering later slots. The maintainer
chooses a distinct identity for an urgent correction.

Prepare matching `pyproject.toml` and `uv.lock` metadata and reviewed notes.
The annotated tag is exactly `v<version>`. The source commit must contain
that version. Keep the existing draft identity while preparing that same
unpublished release unless the maintainer chooses another identity.

Calendar numbering is independent of storage schemas and portable evidence
contracts. Describe incompatible project layouts, supported upgrades, and
required operator actions independently of the application version.

## Qualify and publish a source release

1. Identify the previous reachable release tag and exact included `main` commit.
2. Verify matching project and lockfile versions and draft notes against that
   committed source. Describe implemented behavior and retained evidence.
3. Obtain the complete portable Linux, macOS, and Windows behavior matrix,
   the complete repository gate, default performance checks, and applicable
   Linux server and Windows release checks for the final source.
4. Keep the release in draft when required evidence is unavailable or fails.
   A waiver must name the exact guardrails and the single release it covers.
5. When separately authorized, create and push the annotated `v<version>` tag
   on the verified commit. Verify the remote tag and preserve its identity.
6. When authorized, prepare the GitHub Release with reviewed notes and
   GitHub's automatic source archives. Publish within that authorization.
7. Verify source installation from the tagged revision in a clean environment.
   Preserve the running Impodo session and live Odoo data.

A public release consists of its immutable tag, source commit, reviewed notes,
and automatic GitHub source archives. Custom installers, wheels, containers,
archives, checksums, SBOMs, and other uploaded assets require a separate
policy decision. Accepted internal bundles follow their organizational process.

## Release notes and corrections

Write for data managers and evaluators. Explain the implemented user outcomes,
compatibility boundaries, storage implications, and material limitations.
Link the full source commit, comparison with the previous tag, installation
instructions, and available verification evidence.

Exclude uncommitted changes and proposals from shipped claims. Label draft
notes with their exact candidate. A test of an earlier source does not qualify
a later integrated tree. Report omitted browser, live Odoo, platform, and
hosted checks honestly.

After publication, preserve the source behind the visible tag. Corrections
use another maintainer-approved version and verified commit. A release is
complete when the project version, lockfile, tag, notes, source SHA, and
qualification evidence agree and the published source instructions work.
