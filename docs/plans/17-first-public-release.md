---
audience: developer
kind: plan
status: proposed
---

# First public release and recurring cadence

## Decision and intended reader

Publish Impodo's first GitHub Release now from the next clean and green commit
on `main`. Use version `0.1.0` and tag `v0.1.0`. Attach no custom assets. The
release consists of the Git tag, its release notes, and the source archives
that GitHub automatically exposes for that tag.

After `v0.1.0`, publish weekly releases during the current active-development
period. Move to a monthly release cadence after eight successful source
releases, unless the observed development pace gives a clear reason to change
that transition.

This plan is for the maintainer preparing and publishing releases. It replaces
the earlier proposal for a Windows bundle. Impodo is not introducing a bundle,
installer, standalone wheel download, container package, SBOM, or artifact
attestation at this stage.

## Release now

“Now” means that the first release should not wait for new packaging or release
automation. It needs only one release-preparation change and the existing
repository gate.

The current checkout was reviewed on 2026-10-28. At that point:

- `main` tracked `origin/main` at commit
  `e7f1a23f3153df552843ad9fe00a8662509477ff`;
- `pyproject.toml` still declared version `0.0.0`;
- the repository had no existing Git tags; and
- the worktree contained active uncommitted development.

Do not tag that observed commit merely to satisfy the calendar. Finish and
commit the intended current work, change the project version to `0.1.0`, run
the complete repository gate, and push that exact commit to `main`. The first
release then tags that clean commit as `v0.1.0`.

The release should be published as soon as those bounded checks pass. New
release packaging, security automation, dependency bots, code signing, and
deployment automation are not prerequisites for `v0.1.0`.

## What the release is

The first release gives outside users a stable name for one source revision.
It answers:

- Which exact commit represents the first public release?
- Which changes were included at that point?
- Which version should an issue or test result name?
- Which source should an evaluator check out instead of following a moving
  `main` branch?

The release does not create a new installation method. Current public
evaluation still uses the source-checkout instructions. Accepted internal
bundles remain a separate organizational path. Once the source release is
published and verified, the public instructions can show how an evaluator
selects the release tag rather than `main`.

### Release contents

The GitHub Release contains:

- annotated tag `v0.1.0`;
- release title `Impodo 0.1.0`;
- human-reviewed release notes; and
- GitHub's automatic source code archives for the tag.

The maintainer uploads no files. In particular, the release contains no:

- custom ZIP or bundle;
- wheel or dependency lock as a release asset;
- PowerShell installer;
- Windows, macOS, or Linux package;
- container image;
- generated test report;
- SBOM or vulnerability report;
- checksum file for a custom artifact; or
- artifact attestation.

Git identifies the source release. The tag must continue to point to the same
commit after publication.

### Release status

Use `0.1.0`, not an alpha suffix. Major version zero already communicates that
Impodo is in active development and that interfaces may still change. The
release notes must state that this is the first public development release and
that source installation remains intended for evaluation and development.

Do not describe `0.1.0` as an enterprise distribution, signed application,
offline installer, hosted service, or long-term-support release.

## Immediate `v0.1.0` procedure

### 1. Finish the release commit

The release commit contains all work intentionally included in `0.1.0` and no
unrelated local tooling state. Before the commit:

- confirm that `.agents/` and agent instruction files remain untracked and
  ignored;
- review the complete worktree diff;
- set the `pyproject.toml` version to `0.1.0`;
- confirm that dependency declarations and locks remain coherent; and
- prepare the release notes from the actual commit range.

The version change belongs in the released source. Do not create `v0.1.0`
while the package still identifies itself as `0.0.0`.

### 2. Verify the exact release commit

Run the complete repository gate from the repository root:

```powershell
uv run --frozen --group quality python scripts/check_repository_quality.py
```

Run focused tests for the current development changes as required by their
own responsibilities. Record any host-specific check that was omitted. A
failed required repository guardrail blocks the release unless a human
explicitly names and waives that exact guardrail for this release.

Commit and push only after the gate passes. Confirm that the intended release
commit is the current `origin/main` commit and that the worktree is clean
before creating the tag.

### 3. Prepare release notes

The first release notes should be short enough to scan but specific enough to
set expectations. Include:

1. **What Impodo is.** Summarize the current local Odoo migration workflow.
2. **Why this release exists.** State that `0.1.0` establishes the first named
   public source baseline.
3. **Highlights.** Describe the most important implemented user capabilities.
4. **Supported environment.** Repeat the current Python, operating-system,
   and Odoo boundaries without broadening them.
5. **Installation.** Link the current Windows and macOS source-checkout
   instructions.
6. **Known limitations.** State that there is no packaged installer, automatic
   updater, hosted service, or long-term-support promise.
7. **Reporting problems.** Link GitHub Issues and state what fictional example
   and version information to include.
8. **Source identity.** Link the tag and full release commit.

Use GitHub's generated notes as a starting inventory, then edit them for user
meaning. Do not publish a raw commit list as the complete explanation.

### 4. Create and push the tag

Create an annotated tag on the exact verified commit:

```powershell
git tag -a v0.1.0 -m "Impodo 0.1.0"
git push origin v0.1.0
```

Immediately confirm that the remote tag resolves to the intended `main`
commit. Do not move or reuse the tag after it becomes visible.

### 5. Create the GitHub Release

Create a draft GitHub Release for the existing tag. Attach no files. The
GitHub CLI command may have this shape:

```powershell
gh release create v0.1.0 --draft --generate-notes --title "Impodo 0.1.0" --verify-tag
```

Edit the draft notes to satisfy the release-note contract. Review the tag,
commit, title, notes, and absence of custom assets. Publish the draft as the
latest release.

### 6. Verify as an outside evaluator

After publication:

- open the public Release page without relying on maintainer-only state;
- confirm that `v0.1.0` resolves to the intended commit;
- confirm that the release has no maintainer-uploaded assets;
- download or clone the tagged source into a clean directory;
- follow the current source installation instructions with fictional or
  disposable data; and
- record any instruction that depends on an uncommitted file, private setting,
  or moving `main` branch.

Correct documentation problems through the next reviewed commit. Do not
replace the source represented by `v0.1.0`.

## Release cadence

### Weekly period

The first release date is 2026-10-29. Use Thursday as the release day so that
maintainers have normal working days available for follow-up.

The initial cadence is:

| Release point | Target date |
| --- | --- |
| First release | 2026-10-29 |
| Weekly release 2 | 2026-11-05 |
| Weekly release 3 | 2026-11-12 |
| Weekly release 4 | 2026-11-19 |
| Weekly release 5 | 2026-11-26 |
| Weekly release 6 | 2026-12-03 |
| Weekly release 7 | 2026-12-10 |
| Weekly release 8 | 2026-12-17 |

Each Thursday release names the latest clean and green `main` commit that the
maintainer accepts. Prepare the version and release notes before Thursday so
the release day remains a verification and publication activity.

If no commit has changed since the previous release, do not create a different
version tag on identical source merely to fill the calendar. Record the skipped
release in the release tracking issue and continue the schedule the following
week. The cadence is a regular decision point, not permission to publish
meaningless versions.

### Monthly period

After eight successful source releases, move to the first Thursday of each
month. Under the proposed calendar, the first monthly release is 2027-01-07.

A successful release means:

- the tag and project version agreed;
- the complete repository gate passed on the released commit;
- the release notes accurately described the source;
- the tag was not moved or replaced;
- no custom asset was accidentally attached; and
- no correction was needed to identify which commit had been released.

If the weekly process is still failing or producing emergency replacement
releases, continue weekly until four consecutive releases meet these criteria.
The move to monthly should reduce routine overhead, not hide an unreliable
process.

Monthly is the normal cadence, not a restriction on urgent fixes. Publish a
security or data-integrity correction as soon as it is ready, then keep the
next regular monthly date. Do not delay an urgent fix to preserve calendar
symmetry.

## Version policy

Release cadence and version meaning are separate. Choose the next version from
the changes since the previous tag:

- Increase the patch number for compatible fixes, documentation corrections,
  and internal improvements that do not add a user capability.
- Increase the minor number for a new user capability or a meaningful change
  to the supported workflow.
- Remain below `1.0.0` while public interfaces and migration behavior are still
  expected to change materially.
- Reserve `1.0.0` for a separate stability and support decision.

Examples:

- A week containing only fixes may move `0.1.0` to `0.1.1`.
- A week adding a new supported workflow may move `0.1.1` to `0.2.0`.
- The monthly transition does not itself require a minor-version increase.

Every release must update `pyproject.toml` before tagging. The Git tag is
always `v` followed by that exact project version.

## Lightweight GitHub Actions support

The first release should not wait for new automation. Add automation after
`v0.1.0` to make the weekly cadence repeatable without turning it into a
packaging project.

### Source-release workflow

Add one small `.github/workflows/source-release.yml` workflow before the
second release. It should trigger when a `v*` tag is pushed and should:

1. Check out the exact tagged commit.
2. Verify that the tag equals `v` followed by the `pyproject.toml` version.
3. Verify that the commit is reachable from `main`.
4. Run the complete repository gate for the exact tagged source.
5. Verify that no GitHub Release already uses the tag.
6. Create a draft GitHub Release with generated notes and no custom assets.
7. Leave human review and publication as an explicit maintainer action.

The workflow needs `contents: write` only in the job that creates the draft.
All verification jobs use `contents: read`. Do not grant package,
attestation, deployment, environment, issue, or pull-request write permission.

Use the GitHub CLI already available on hosted runners rather than adding a
third-party release action merely to call the Releases API. Keep checkout and
other actions pinned to complete commit SHAs.

### Cadence reminder

Add a scheduled read-only workflow only if maintainers need a reminder. A
Thursday schedule may report:

- the latest release tag and date;
- commits on `main` since that tag;
- whether `pyproject.toml` has a newer version;
- the latest `Python quality / quality` result; and
- the next expected release date.

The reminder must not create tags, change versions, or publish releases. A
maintainer chooses the version and confirms the notes because those decisions
depend on the meaning of the changes.

## Release notes template

Use this structure for weekly and monthly releases:

```markdown
# Impodo <version>

## Release summary

One paragraph describing why this release matters to an Impodo evaluator.

## Highlights

- Describe implemented user-facing outcomes.
- Describe important reliability, security, or evidence improvements.

## Compatibility

- State supported Python, operating-system, and Odoo boundaries.
- Name any project-data or workflow compatibility concern.

## Install from source

Link the Windows and macOS instructions and identify the exact release tag.

## Known limitations

State current limitations honestly, including the absence of packaged assets.

## Report a problem

Link GitHub Issues and request the Impodo version, Odoo version, expected
behavior, actual behavior, and a small fictional example.

## Source

Link the tag, full commit, comparison with the previous release, and CI run.
```

Do not copy unreleased roadmap claims into release notes. Notes describe only
behavior present in the tagged source.

## Failure and correction policy

### Before publication

If the repository gate, version check, or release-note review fails, do not
publish the Release. Correct the source through a normal commit and run the
gate again.

Avoid pushing the tag until the source and notes are ready. If a tag has been
pushed publicly and then proves incorrect, do not silently move it. Use the
next version for the corrected release.

### After publication

Never replace the source behind a published tag. For an ordinary defect,
record the issue and include the correction in the next weekly or monthly
release.

For a defect that can compromise security, authorization, source evidence,
reviewed changes, execution journaling, or reconciliation:

- warn users on the affected Release page;
- prepare and verify the correction immediately;
- publish a new patch release without waiting for the next scheduled date; and
- link the affected and replacement releases.

Changing release notes to add a warning is acceptable. Moving the tag or
pretending the affected source was never released is not.

## Implementation sequence

### Step 1: publish `v0.1.0` now

- Finish the intended current work.
- Change `pyproject.toml` from `0.0.0` to `0.1.0`.
- Draft notes from implemented behavior.
- Run focused checks and the complete repository gate.
- Commit and push the clean release source.
- Create annotated tag `v0.1.0`.
- Create a draft GitHub Release with no custom assets.
- Review and publish it.
- Verify the public source release from a clean directory.

Exit criteria:

- the project version, tag, title, and notes all say `0.1.0`;
- the tag resolves to the intended green `main` commit;
- the Release contains no custom asset;
- the released source can be installed through the current documented route;
  and
- an issue can name `0.1.0` as a fixed source baseline.

### Step 2: make the second release repeatable

- Add the minimal tag-triggered source-release workflow.
- Protect `v*` tags from updates and deletion.
- Add the release-note template.
- Exercise the workflow with the second weekly version.

Exit criteria:

- the workflow validates the version and tagged commit;
- only the draft-creation job has release write permission;
- no workflow step builds or uploads an artifact; and
- human review remains required before publication.

### Step 3: complete the weekly runway

- Publish on the seven Thursday dates after `v0.1.0` when source changed.
- Record skipped dates rather than tagging identical source.
- Track preparation time, failed gates, note corrections, and emergency
  releases.
- Keep current documentation aligned with each released source version.

Exit criteria:

- eight releases have a consistent tag, version, notes, and CI contract; or
- after a skipped week, the release tracking record still explains the
  cadence clearly; and
- four consecutive releases require no identity or process correction.

### Step 4: switch to monthly

- Set the first Thursday as the regular release day.
- Keep urgent corrections outside the calendar when necessary.
- Review the cadence after three monthly releases.

Exit criteria:

- outside users can predict the next normal release window;
- active development is summarized at least monthly; and
- the reduced cadence does not accumulate release notes that are too large to
  review meaningfully.

## Acceptance criteria

This plan is working when:

- `v0.1.0` is published now from the next clean and green `main` commit;
- no custom release asset is uploaded;
- the project version and tag always agree;
- every release identifies one immutable source commit;
- every released commit passed the complete repository gate;
- notes explain implemented outcomes and current limitations;
- weekly release decisions occur through 2026-12-17;
- the normal cadence becomes monthly on 2027-01-07 if the weekly process is
  reliable;
- urgent security and data-integrity fixes are not delayed by the schedule;
  and
- future packaging remains a separate decision rather than entering this
  source-release workflow indirectly.

## Explicit non-goals

This plan does not authorize or require:

- a release bundle or custom archive;
- a standalone wheel asset or PyPI publication;
- a PowerShell installer;
- a macOS package;
- a container image or GitHub package;
- an SBOM or artifact attestation for a source-only release;
- code signing;
- an offline installation;
- automatic version selection;
- fully automatic release publication;
- automatic deployment; or
- a promise that every scheduled date will receive a new tag when the source
  has not changed.

These capabilities need a later decision based on an actual distribution or
deployment requirement.

## GitHub references

Use GitHub's
[release management guidance](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository?tool=cli)
for draft creation, generated notes, and publication. Use
[repository rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository)
to protect `main` and published version tags.

Re-check GitHub feature availability while implementing automation. This plan
was reviewed on 2026-10-29.
