---
audience: developer
kind: plan
status: proposed
---

# GitHub Actions public trust and release evidence

## Decision and intended reader

Use GitHub Actions to make Impodo's engineering claims independently
verifiable. Keep the current complete repository gate and platform-specific
tests, then add protected merge policy, public security signals, and governed
release workflows. Begin with source-only GitHub Releases. Add packaged
artifacts, signed provenance, and a Software Bill of Materials (SBOM) only
when Impodo intentionally introduces a packaged distribution.

This proposal is for maintainers deciding how to turn the existing continuous
integration foundation into credible public evidence. It does not describe
current release or deployment behavior. No workflow, repository setting,
release channel, or deployment described as proposed below exists merely
because it appears in this plan.

## Release-first sequencing decision

Start with the source-only GitHub Release described in the
[first public release plan](17-first-public-release.md). Publish the first
release immediately from the next clean and green `main` commit, then use a
weekly cadence before moving to monthly releases. Attach no custom artifact.
Resume the broader pull-request, packaged-release, container, and deployment
work in this plan after the source-release cadence is repeatable.

The primary outcome is not “more automation.” An evaluator should be able to
answer these questions from GitHub without trusting a maintainer's private
process:

1. Was this change reviewed and tested before it reached `main`?
2. Which portable, platform-specific, and security checks passed?
3. Which source commit produced this downloadable artifact or container?
4. Can the artifact, provenance, and dependency inventory be verified?
5. How are vulnerabilities reported, triaged, and corrected?
6. Was a deployment promoted from the tested release rather than rebuilt?

## Recommendation in one page

Implement the work in this order:

1. Publish the source-only `v0.1.0` GitHub Release described in the
   [first public release plan](17-first-public-release.md), with no custom
   assets, and establish its weekly-then-monthly cadence.
2. Protect `main` with a public ruleset that requires a pull request, one
   stable aggregate CI check, resolved review conversations, and no force
   pushes or deletions.
3. Preserve the existing `Python quality / quality` check name while
   introducing one always-created pull-request trust workflow. The aggregate
   job should fail when any applicable portable, Linux server, Windows
   release, dependency, or container check fails.
4. Enable GitHub's security signals for the public repository: CodeQL,
   dependency review, Dependabot alerts, secret scanning, and private
   vulnerability reporting. Add version updates for GitHub Actions and Docker;
   automate Python updates only after confirming coherent `pyproject.toml` and
   `uv.lock` changes.
5. Add `SECURITY.md` and a concise contribution contract before advertising
   the project as externally supported.
6. Publish the server image to GitHub Container Registry only when the Linux
   server profile is supported for external evaluation. Deploy by immutable
   digest through protected GitHub environments; never rebuild during
   promotion.
7. Add only a small set of README badges after their underlying controls are
   required and reliable: aggregate CI, CodeQL, latest release, and optionally
   OpenSSF Scorecard or Best Practices after the project has earned a useful
   result.

The source-only public release is the immediate priority because the
repository currently exposes source but no named release. The broader CI and
security program then strengthens the process that feeds later releases and
any future packaged distribution.

## Current baseline observed on 2026-10-28

Impodo already has stronger foundations than a typical early public project.
The checkout contains three GitHub Actions workflows:

| Workflow | Current evidence | Current limitation |
| --- | --- | --- |
| `python-quality.yml` | Runs the complete repository quality gate on Ubuntu, macOS, and Windows for every pull request and push to `main`. It keeps the historical `quality` aggregate job. | The check is credible only if repository rules actually require it. The checkout cannot prove those GitHub settings. |
| `linux-server-quality.yml` | Runs Linux-owned PostgreSQL and server contracts, builds the container, scans it with Trivy, generates BuildKit provenance and an SPDX SBOM, and retains evidence for 30 days. | Path filters can omit the whole workflow. Its uploaded evidence is diagnostic and temporary, not a signed, downloadable release subject. |
| `windows-release-quality.yml` | Exercises the Windows internal-release path and its owned tests on pull requests that match selected paths or by manual dispatch. | It does not run on pushes to `main`, version tags, or a public release event, and it publishes no release asset. |

The workflow dependencies are already pinned to full commit SHAs, permissions
are read-only by default, the Python environment is frozen, and the internal
release tooling already produces tests, a dependency audit, a secret scan, an
SBOM, and hashed manifest evidence. These controls should be retained.

The public GitHub repository currently shows source, documentation, and an MIT
license, but no published Release, package, or visible security policy. The
checkout also has no Dependabot configuration, CodeQL workflow, dependency
review workflow, `SECURITY.md`, or `CODEOWNERS`. GitHub settings such as branch
rules, secret scanning, private vulnerability reporting, and Dependabot alerts
are not stored in the checkout and must be audited in the repository UI or API
before implementation begins.

## Credibility model

Every public claim should have four parts:

```text
claim -> enforced control -> retained evidence -> consumer verification
```

For example, “this is the tested Impodo 1.0 server image” is credible only if:

- a protected tag or release refers to a reviewed commit;
- one workflow builds the image after all required checks pass;
- the image is published by digest with an SBOM and signed provenance; and
- a consumer can run a documented verification command against the repository
  identity and expected tag or commit.

Badges, green checks, and uploaded logs are useful navigation aids. They are
not substitutes for enforced policy or verifiable release identity.

### Proposed evidence map

| Public claim | Enforced control | Retained public evidence | Verification |
| --- | --- | --- | --- |
| Changes on `main` passed review and CI. | Branch ruleset requires a pull request, review, and the aggregate trust check. | Pull request, review history, check runs, and immutable commit SHA. | Inspect the commit and ruleset; follow the required check to its run. |
| Impodo remains portable across supported developer platforms. | The current complete gate continues on Ubuntu, macOS, and Windows. | Matrix check results and concise job summaries. | Open the aggregate run and inspect every matrix result. |
| Linux server and Windows release paths passed when affected. | A repository-owned change classifier selects the relevant lanes; the aggregate check validates both execution and justified skips. | Per-lane summaries with the classified change set and outcomes. | Inspect the aggregate summary and linked lane results. |
| Dependencies and source are continuously examined. | CodeQL, dependency review, Dependabot, secret scanning, and container scanning. | GitHub Security results, pull-request dependency diffs, alerts, and workflow results. | Inspect the Security tab and the dependency review check. |
| A release came from this repository and workflow. | Protected release workflow with minimal write permissions and GitHub OIDC. | GitHub Release, immutable asset digest, and artifact attestation. | Run `gh attestation verify` against the expected repository. |
| Consumers can inspect release contents. | Release job creates and validates the SBOM before publication. | SPDX or CycloneDX SBOM attached to or attested with the release. | Verify the SBOM attestation and inspect the dependency inventory. |
| Production uses what CI tested. | Deployment consumes the published digest through a protected environment. | GitHub deployment record, environment approval, and smoke-test result. | Match the deployed digest to the release and attestation. |

## Target GitHub Actions design

Use four clearly separated workflow responsibilities. Reusable workflows may
remove mechanical duplication, but the required check and release authority
must remain easy for an outside reviewer to follow.

```text
pull request
    |
    `-- PR trust / required
        |-- complete portable gate: Ubuntu, macOS, Windows
        |-- Linux server contracts, when applicable
        |-- Windows release contracts, when applicable
        |-- dependency review, when applicable
        `-- container build and scan, when applicable

merge to main
    |
    `-- mainline evidence
        |-- complete gate on the merged commit
        |-- affected platform and image lanes
        `-- failure evidence and maintainer notification

schedule or manual dispatch
    |
    `-- scheduled assurance
        |-- slow integration and scenario lanes
        |-- dependency and container drift
        `-- measured performance or compatibility checks

protected version tag or release approval
    |
    `-- release
        |-- consume the tested commit
        |-- build each artifact once
        |-- audit, scan, and generate SBOMs
        |-- publish GitHub Release and optional GHCR image
        `-- attest artifact and SBOM identities
```

### Pull-request trust workflow

Create one workflow that starts for every pull request. Do not put a path
filter on the workflow itself. A small repository-owned classifier should
determine which specialized lanes apply and expose its decision as job
outputs. The final aggregate job should use `if: always()` and fail when:

- a required lane failed, was cancelled, or never produced an expected result;
- a specialized lane was skipped without the classifier declaring it
  inapplicable; or
- the classifier itself failed.

Preserve `Python quality / quality` as the required check name during the
transition, or change the ruleset and workflow atomically. Renaming a required
check without changing repository rules can silently block every pull request.

The workflow should include:

- the complete current repository gate on Ubuntu, macOS, and Windows;
- Linux server tests and the container scan for server, packaging,
  dependency, and container changes;
- Windows release tests for release tooling, runtime, packaging, and
  dependency changes;
- dependency review for changes that alter dependency declarations or lock
  files;
- explicit timeouts on every job;
- concurrency cancellation for superseded runs on the same pull request; and
- a Markdown step summary that states the source SHA, applicable lanes,
  passed checks, justified skips, and links to retained evidence.

Run fork pull requests with the `pull_request` event and no repository secrets.
Do not use `pull_request_target` to check out and execute untrusted contributor
code. Release, package, and deployment permissions must not exist in this
workflow.

### Mainline evidence workflow

Run the complete gate again on the merged `main` commit. Pull-request success
refers to GitHub's test merge; mainline evidence proves the exact commit that
will be released. Retain failed-test reports, sanitized logs, and scanner
reports long enough to diagnose regressions, but do not upload project data,
Odoo credentials, database files, or customer-derived evidence.

Use mainline failures as actionable incidents:

- create or update one tracking issue only if the failure persists and can be
  deduplicated reliably;
- label the failing commit or release candidate as blocked;
- fix forward or revert; and
- never rerun repeatedly until a flaky test happens to pass.

Avoid creating a second full implementation of the same checks. Extract
reusable workflows for the portable gate and specialized lanes while keeping
their callers and permissions explicit.

### Scheduled assurance workflow

Scheduled CI should find drift that a pull request cannot reveal. Start with a
weekly schedule and manual dispatch, then adjust frequency from observed value
and cost. Suitable scheduled work includes:

- complete dependency and vulnerability rescans;
- building the current `main` container against newly available base-image
  metadata;
- longer generated-data scenarios and supported Odoo compatibility lanes;
- performance trend capture with stable workloads; and
- release-rehearsal verification, including SBOM generation and manifest
  validation, without publishing a release.

Scheduled failures should produce an issue or alert with a stable fingerprint,
the first failing SHA, the last passing run, and the owning workflow. Do not
make time-sensitive external services part of the required pull-request gate
unless they have a deterministic local substitute.

### Release workflow

Trigger a release only from a protected semantic-version tag or a manually
approved release environment after the exact `main` commit has passed mainline
evidence. The release workflow should:

1. Verify the tag, version metadata, clean source state, and required upstream
   workflow conclusion.
2. Build the Windows release bundle and, when publicly supported, the server
   container exactly once.
3. Run the existing test, dependency-audit, secret-scan, manifest, and SBOM
   controls on the release subjects.
4. Scan the final container digest, not only an intermediate local tag.
5. Publish the Windows asset, checksums, SBOM, support statement, and release
   notes to a GitHub Release.
6. Publish the server image to `ghcr.io` by immutable digest and add readable
   version and commit tags as references to that digest.
7. Generate GitHub artifact attestations for each downloadable executable
   bundle or manifest and each published container digest. Attach an SBOM
   attestation to the corresponding subject.
8. Verify the published artifact and attestation from a separate final job.
9. Mark a release complete only after consumer-style installation or smoke
   verification succeeds.

GitHub artifact attestations link a release subject to the repository,
workflow, event, environment, and commit. For a public repository, GitHub
records the Sigstore material in a public transparency log. This supports
SLSA v1.0 Build Level 2 provenance, but it does not prove that the artifact is
safe. Tests, review, vulnerability management, and consumer policy remain
separate controls.

Document one verification command beside every public release. For example,
the final command for a downloaded asset should have this shape:

```powershell
gh attestation verify <downloaded-release-asset> -R fderyckel/dev-impodo
```

The container command should verify the immutable `ghcr.io` digest rather
than trusting a mutable tag. The implemented documentation must substitute the
actual published asset and image names and include checksum verification.

### Deployment and promotion workflow

Do not add “continuous deployment” until Impodo has a supported externally
operated server environment. When that environment exists, keep deployment
separate from build and release:

- select an already published and attested digest;
- require approval through a GitHub environment for Production;
- use OpenID Connect and short-lived cloud credentials instead of long-lived
  deployment secrets where the target platform supports it;
- restrict environment secrets and deployment branches or tags;
- record the release digest, configuration revision, approver, and smoke-test
  result; and
- roll back by promoting a previously verified digest.

A deployment workflow must not compile, resolve dependencies, or rebuild an
image. Rebuilding breaks the identity link between the tested release and the
running software.

## Repository protection and review policy

Create a branch ruleset for `main`. The recommended starting policy is:

- require a pull request for every change;
- require the stable aggregate trust check;
- require one approval from someone other than the latest pusher when a second
  maintainer is available;
- dismiss stale approvals after material changes;
- require all review conversations to be resolved;
- require the branch to be current before merging, unless a tested merge queue
  later owns this guarantee;
- block force pushes and branch deletion;
- restrict bypass to an emergency maintainer role, with every bypass explained
  in a follow-up issue; and
- require CodeQL results after the initial scan is stable and correctly
  configured.

Rulesets are public evidence: anyone with read access can inspect active
repository rules. Start in evaluate mode if needed, inspect the effects, and
then enforce the rules. Keep an exported or scripted settings audit in the
maintenance process because most repository settings are not versioned with
the checkout.

Do not require signed commits immediately unless all maintainers and automation
can satisfy the rule without routine bypasses. Signed release provenance and a
protected merge trail give greater immediate value. Revisit commit signing
after the contribution flow has been rehearsed.

A merge queue is useful only when simultaneous pull requests regularly invalidate
each other's tested merge state. GitHub merge queues for public repositories
require organization ownership, and a queued workflow must also trigger on
`merge_group`. The repository is currently under a personal account, so moving
to an organization and observing enough merge contention are prerequisites,
not immediate actions.

## Security automation

### Code and secret scanning

Enable CodeQL default setup for Python first. Review its initial findings and
establish ownership before making code-scanning results required. Add custom
configuration only when default setup cannot represent the actual build or
exclusions; avoid a large hand-written workflow without evidence that it finds
more useful results.

Enable secret scanning, push protection where available, and private
vulnerability reporting. Keep the existing repository secret scan as a
complementary release control because it checks the packaged release context.
If any credential is detected, revoke it before suppressing or resolving the
alert.

### Dependency review and update automation

Add the GitHub dependency review action to pull requests and fail on newly
introduced dependencies with unacceptable known vulnerabilities or licenses.
The policy should document severity, exploitability, runtime reachability,
available fixes, and exception expiry rather than treating every alert as
equally urgent.

Add `.github/dependabot.yml` with small, grouped update streams for:

- GitHub Actions, preserving full-SHA pins and same-line version comments;
- Docker base images; and
- Python dependencies only after a trial proves that the bot produces a
  coherent `pyproject.toml` and `uv.lock` update under the pinned `uv`
  toolchain.

If Dependabot cannot maintain the Python lock coherently, use a scheduled,
repository-owned `uv lock --upgrade-package ...` workflow that opens a pull
request through a narrowly scoped GitHub App or token. Do not accept a bot
change that modifies declarations without the frozen lock or passes only after
unfrozen dependency resolution.

Set a predictable cadence, such as weekly grouped non-security updates and
immediate security pull requests. Do not auto-merge dependency changes until
the full platform matrix, specialized lanes, and rollback history demonstrate
that the policy is safe.

### Action and workflow hardening

Retain the controls already present and make them repository policy:

- pin every third-party action to a full commit SHA and keep a readable version
  comment;
- enable GitHub's setting that requires full-length action SHA pins if it is
  available for the repository plan;
- set top-level permissions to read-only and grant `contents: write`,
  `packages: write`, `attestations: write`, or `id-token: write` only to the
  exact release job that needs each capability;
- set job timeouts and concurrency deliberately;
- prefer GitHub-hosted runners for untrusted pull requests;
- never expose release environments or credentials to forked pull requests;
- review the source and permission changes of every action update; and
- avoid dynamic shell construction from pull-request titles, branch names,
  labels, or other untrusted context.

The existing action SHA pins are a real public trust signal. Dependabot should
keep them current without weakening them to moving major-version tags.

## Public trust surfaces beyond workflows

CI results are difficult for a new evaluator to interpret without a small
public contract. Add these files with the implementation that makes their
claims true:

- `SECURITY.md`: supported versions, private reporting route, expected first
  response, disclosure coordination, and what evidence reporters should avoid
  uploading publicly;
- `CONTRIBUTING.md`: supported setup, exact local and repository gates,
  focused-test expectations, review process, and fictional-data requirement;
- `.github/CODEOWNERS`: ownership of workflows, release tooling, security
  policy, and high-risk persistence or execution boundaries, once at least two
  practical reviewers exist; and
- release notes or a changelog generated from reviewed pull-request labels,
  with human-edited user impact, compatibility, upgrade, and known-limit
  sections.

Use a restrained README badge row only after enforcement is active:

1. aggregate repository quality;
2. CodeQL or code scanning;
3. latest stable release; and
4. an external OpenSSF signal after its result is understood and maintained.

Do not add badges for every matrix lane. A badge must link to the evidence an
evaluator needs, and a passing badge must correspond to a maintained control.

Run the OpenSSF Scorecard action as a non-blocking, scheduled assessment. Use
its findings to improve policy, not to chase a score or make a weak result
prominent. Pursue the OpenSSF Best Practices badge once `SECURITY.md`, release
management, contribution guidance, and automated tests are publicly current.

## Coverage and test evidence

Do not use one repository-wide coverage percentage as the main quality claim.
Impodo's credibility depends more on evidence preservation, authorization,
deterministic ordering, recovery, and Odoo integration contracts than on a
single percentage.

Introduce coverage in two stages:

1. Publish an informational report from the canonical Ubuntu test run and
   retain it long enough to establish a stable baseline.
2. After classifying generated files and integration boundaries, enforce
   reviewed thresholds on changed code or critical packages. Any ratchet must
   have a documented exception process and must not encourage low-value tests.

Expose test counts, durations, skipped-test reasons, and flaky retries in the
job summary. A skipped platform or integration lane needs an explicit owner and
reason. Automatic retries should be diagnostic and should not turn a first
failure into an unqualified green result.

## Cost and feedback-time policy

The current complete three-operating-system gate is expensive but directly
supports Impodo's portability claim. Keep it while measuring queue time,
execution time, and failure yield. Apply these optimizations first:

- cancel superseded pull-request runs;
- cache only verified dependency downloads, never mutable build outputs;
- keep `uv --frozen` and repository-local toolchain ownership;
- run independent lanes in parallel;
- use change classification only for genuinely specialized server, image, and
  release lanes; and
- move slow compatibility and long scenarios to scheduled assurance unless
  the affected change makes them required.

Consider replacing the full macOS and Windows gate with focused platform
contracts only after those tests demonstrably own the same portability risks.
Do not deduplicate merely to obtain a faster badge.

Set an initial feedback objective rather than a hard promise:

| Signal | Initial objective |
| --- | --- |
| Pull-request aggregate result | Useful first failure within 10 minutes; complete result within 25 minutes at the 90th percentile. |
| Flaky pass rate | Zero silent retry-to-green results; each first-attempt failure remains visible. |
| Mainline regression response | Owner notified in the same workday; release remains blocked. |
| Critical exploitable vulnerability | Triage within one business day; remediation or documented containment within a defined support-policy window. |
| Release verification | Every published artifact and image has a successful consumer-style attestation check. |

Measure for four weeks before changing targets.

## Delivery phases

### Phase 0: audit and protect

This phase changes repository settings and records the baseline before adding
automation.

- inventory branch protection, Actions permissions, fork settings, security
  features, environments, packages, and release settings;
- create a `main` ruleset in evaluate mode;
- confirm the exact current aggregate check name and then enforce it;
- enable Dependabot alerts, secret scanning, push protection, and private
  vulnerability reporting where the public plan supports them; and
- capture screenshots or API output for maintainer records without committing
  secrets or administrative identifiers.

Exit criteria:

- direct unreviewed changes cannot reach `main` without an explicit recorded
  bypass;
- the existing aggregate check is required and observed on a test pull
  request; and
- the repository security feature inventory has an owner and review date.

### Phase 1: make pull-request trust legible

- add the always-created pull-request trust workflow and classifier;
- preserve the current required check contract;
- add dependency review, CodeQL default setup, and Dependabot for Actions and
  Docker;
- add concurrency, timeouts, and concise job summaries;
- add `SECURITY.md` and contribution guidance; and
- run OpenSSF Scorecard non-blocking to create a prioritized improvement list.

Exit criteria:

- every pull request produces one unambiguous aggregate result;
- applicable and skipped lanes are visible and justified;
- fork pull requests receive no secrets or write permissions;
- newly introduced vulnerable dependencies are reviewed; and
- outsiders can find a private vulnerability-reporting path.

### Phase 2: make mainline and scheduled evidence durable

- refactor duplicated mechanics into reviewed reusable workflows;
- add exact-merge mainline evidence;
- add scheduled dependency, image, compatibility, and release-rehearsal lanes;
- retain sanitized failure artifacts and scanner reports under an explicit
  retention policy; and
- measure feedback time, failure yield, and flakiness for four weeks.

Exit criteria:

- the latest `main` commit has complete, navigable evidence;
- scheduled failures have one deduplicated owner path; and
- optimization decisions use measured cost and fault-detection value.

### Phase 3: publish verifiable releases

- define the public version and support policy;
- protect release tags and the release environment;
- publish a GitHub Release built from an exact passing `main` commit;
- publish checksums, SBOMs, and GitHub artifact attestations;
- publish the server image to GHCR only if its support statement is current;
- verify assets and image digests in a separate final job; and
- document download, checksum, SBOM, and attestation verification.

Exit criteria:

- a clean machine can download, verify, and launch the supported release path;
- every release subject maps to one source commit and workflow run;
- mutable tags are not used as the authority for deployment or verification;
  and
- a failed verification prevents the release from being presented as stable.

### Phase 4: promote rather than rebuild

Begin only when an externally operated server deployment is supported.

- create Staging and Production environments with scoped reviewers;
- use OIDC or an equivalently short-lived credential mechanism;
- promote an already attested digest;
- run post-deployment smoke and rollback checks; and
- expose the deployment record beside the release.

Exit criteria:

- the deployed digest equals the tested release digest;
- Production requires an explicit human or governed policy approval;
- rollback selects a previously verified digest; and
- no deployment credential is available to pull-request jobs.

## Proposed workflow ownership

| Responsibility | Proposed file or setting | Required for merge? | Write authority |
| --- | --- | --- | --- |
| Aggregate pull-request trust | `.github/workflows/pr-trust.yml` or an evolved `python-quality.yml` | Yes | None |
| Reusable portable gate | `.github/workflows/reusable-portable-quality.yml` | Through caller | None |
| Reusable Linux/server and image validation | `.github/workflows/reusable-linux-server-quality.yml` | When classifier selects it | None |
| Reusable Windows release validation | `.github/workflows/reusable-windows-release-quality.yml` | When classifier selects it | None |
| CodeQL | GitHub default setup initially | After baseline stabilization | Security results only |
| Dependency review | Pull-request trust lane | When dependency files change, preferably all pull requests if cost is low | None |
| Mainline evidence | `.github/workflows/mainline-evidence.yml` | No; blocks release | Diagnostic artifact upload only |
| Scheduled assurance | `.github/workflows/scheduled-assurance.yml` | No; blocks release while unresolved | Diagnostic artifact upload and bounded issue update |
| Release | `.github/workflows/release.yml` | Not applicable | Release, package, attestation, and OIDC only in scoped jobs |
| Deployment | `.github/workflows/deploy.yml`, later | Not applicable | Protected environment and OIDC only |

The exact filenames are proposals. Preserve stable public check names even if
workflow internals are reorganized.

## Explicit non-goals and rejected shortcuts

This proposal does not authorize:

- automatic deployment of pull requests or arbitrary `main` commits;
- use of customer, migration, database, credential, or protected evidence in
  public CI;
- repository secrets in forked pull-request jobs;
- moving action references from full SHAs back to mutable tags;
- attesting every test build or ordinary source file;
- treating provenance as proof that a release is vulnerability-free;
- rebuilding a release during deployment;
- auto-merging dependency updates before the full matrix and specialized lanes
  prove the policy safe;
- requiring a coverage number before its scope and baseline are trustworthy;
- rerunning failures until the status becomes green; or
- adding a badge whose underlying check is optional, unreliable, or
  misleading.

## Success measures

Review these measures quarterly and publish only the ones that are meaningful
to users:

- percentage of `main` commits introduced through the protected pull-request
  path;
- percentage of stable releases with verified provenance and SBOM attestations;
- median and 90th-percentile pull-request feedback time;
- first-attempt pass rate and unresolved flaky-test count;
- mean time to triage and remediate supported security alerts;
- percentage of dependencies covered by automated update ownership;
- number of releases successfully verified on a clean machine; and
- number of deployments whose digest exactly matches a verified release.

Avoid counting workflow runs, badges, or raw test totals as credibility
outcomes. They measure activity, not trust.

## External standards and GitHub references

Use these sources when implementing the proposal:

- [GitHub ruleset capabilities](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
  for required pull requests, checks, code scanning, and force-push protection;
- [GitHub artifact attestation concepts](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
  for provenance scope, SLSA level, Sigstore transparency, and limitations;
- [GitHub artifact attestation implementation](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
  for scoped permissions, artifact and container subjects, SBOM attestations,
  and verification commands;
- [GitHub dependency review](https://docs.github.com/en/code-security/concepts/supply-chain-security/dependency-review)
  for pull-request dependency risk;
- [GitHub Dependabot version updates](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-version-updates)
  and [supported ecosystems](https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories)
  for automated update scope;
- [GitHub Actions secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use)
  for immutable action references and untrusted input handling;
- [OpenSSF Scorecard action](https://github.com/ossf/scorecard-action) for an
  external, non-blocking supply-chain assessment; and
- [OpenSSF Best Practices](https://www.bestpractices.dev/en) for the later
  public self-certification milestone.

Re-check GitHub plan availability and current documentation during
implementation. Product features and limits can change after this proposal's
2026-10-28 review date.
