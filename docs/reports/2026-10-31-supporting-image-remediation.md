---
audience: developer
document_type: report
status: current
---

# Supporting image remediation

## Result and scope

The repository-owned PostgreSQL and Caddy candidates pass the existing
fixable-critical/high vulnerability policy on both `linux/arm64` and
`linux/amd64`. They preserve PostgreSQL 18.6 and Caddy 2.11.7. This resolves
the two supporting-image failures reported from
[GitHub Actions run 41219549221](https://github.com/fderyckel/dev-impodo/actions/runs/41219549221).
This session did not trigger or verify a hosted rerun of the replacement
workflow.

Unfixed findings remain visible and need release review. This result qualifies
local candidate builds and their container behavior. It does not accept a
shared release, a remote host, production versions, or licenses.

The work remained on `main`. This session created no branch, commit, merge,
push, or registry publication. Concurrent application, storage-baseline, and
documentation changes were preserved, including another session's checkpoint
while this verification ran. The image repair changes deployment builds, CI,
focused tests, and their documentation.

## Implemented builds

- [PostgreSQL](../../deploy/images/postgres/Dockerfile) derives from the same
  digest-pinned official Bookworm image. It replaces only `gosu` with version
  1.19 compiled using digest-pinned Go 1.27.3. The source archive is verified
  against SHA-256
  `cd9719b775dbfedae53923c9b0dc792b66d42c51e0b36652ed6f747fbadc0164`.
- [Caddy](../../deploy/images/caddy/Dockerfile) derives from the same
  digest-pinned official Alpine image. Its standard-module entry point builds
  Caddy 2.11.7 with Go 1.27.3, `go-jose/v4` 4.1.6, and `x/net` 0.60.0. Its
  reviewed module graph and checksums are stored with the build. Runtime
  Brotli is pinned to 1.2.1-r0 and curl/libcurl to 8.23.0-r0.
- The builds disable automatic Go toolchain downloads and refuse module-file
  changes during compilation. Both include the rebuilt component's license
  and Go's license. The separate SPDX inventory supports release review.
- Compose builds these local outputs before use and sets `pull_policy: never`
  on their services. The provisioner shares the PostgreSQL output. The Docker
  build inputs retain explicit versions, image digests, and source checksums;
  the local output tags are not registry release digests.
- The Linux workflow builds these same images before running its live tests.
  The supporting-image matrix builds each candidate, retains the complete
  critical/high scan, enforces the unchanged rejection policy, and exports
  maximum-mode provenance and an SPDX SBOM after a passing scan.

The [build reference](../../deploy/images/README.md) records the commands and
maintenance responsibilities. No vulnerability exclusion or policy waiver
was introduced.

## Exact local images

| Tag | Architecture | Image ID |
| --- | --- | --- |
| `impodo-postgres:18.6-impodo1` | arm64 | `sha256:9850a93eb8ded54a5b985f5f295fe3a0092b74b6c826b6208c5689b538a26a64` |
| `impodo-caddy:2.11.7-impodo1` | arm64 | `sha256:e746acd50e13f2ac5f6dc1ff6040dccfb96a983cb3aa58f8137afba7a0530dd3` |
| `impodo-postgres:18.6-impodo1-amd64` | amd64 | `sha256:7075ea04c801de816654419d8f24996a8c898002a8884dd03138d226669ad27f` |
| `impodo-caddy:2.11.7-impodo1-amd64` | amd64 | `sha256:eb789d9bc06bb8ec6728f46f348d5336a9a3ace518076b1104cde8a104e9b326` |

The isolated trial application is the separate arm64 image
`sha256:de0cd5601916e71f8921eadff4245ec74866bc26d26893e12951e40e4baa6e3b`,
tagged `impodo-server:patched-image-rehearsal`. It was built from the current
checkout without replacing the existing trial's application tag.

## Security scan evidence

Pinned Trivy 0.76.1 collected all critical and high alerts and then ran the
exact CI rejection command with `--exit-code 1 --ignore-unfixed`. Both final
architectures produced the following results. Counts are package alerts,
including repeated CVEs across packages, rather than distinct vulnerabilities
or proven exploits.

| Candidate | Critical alerts | High alerts | Fixable critical/high alerts | Policy exit |
| --- | ---: | ---: | ---: | ---: |
| PostgreSQL arm64 | 3 | 69 | 0 | 0 |
| PostgreSQL amd64 | 3 | 69 | 0 | 0 |
| Caddy arm64 | 0 | 1 | 0 | 0 |
| Caddy amd64 | 0 | 1 | 0 | 0 |

The previous failing pins had 26 fixable alerts in `gosu` and nine in Caddy's
runtime packages and Go dependencies. The rebuilt candidates remove those
alerts without changing the configured policy.

PostgreSQL still reports unfixed critical alerts for SQLite
`CVE-2025-7458`, libxml2 `CVE-2026-6653`, and zlib `CVE-2023-45853`, alongside
unfixed high alerts. Caddy retains the unfixed `go-jose/v3`
`CVE-2026-116587` alert; its v4 dependency is patched. A passing fixable-alert
gate does not resolve reachability, upstream classification, or release
acceptance for these findings.

Full JSON and exact gate outputs are retained locally under these prefixes:

- `/tmp/impodo-patched-postgres-scan.json` and `-gate.log`.
- `/tmp/impodo-patched-postgres-amd64-scan.json` and `-gate.log`.
- `/tmp/impodo-patched-caddy-scan.json` and `-gate.log`.
- `/tmp/impodo-patched-caddy-amd64-scan.json` and `-gate.log`.
- `/tmp/impodo-patched-final-scans.log` summarizes all four final checks.

## Functional and build verification

| Check | Result |
| --- | --- |
| Focused architecture contracts | 14 passed; 41 subtests passed. |
| Linux-owned suite on a disposable patched PostgreSQL database | All 31 tests passed on the final images, including the strengthened PID assertion and the real 37,253-row PLW preparation/replay case. |
| Linux workflow's server integration command | All 99 tests passed. |
| `gosu` behavior | Its version identifies Go 1.27.3; it drops to UID 65534 and preserves PID 1 through exec. |
| Non-root Caddy | Version, patched build dependencies, required modules, and patched curl passed with UID 10001 and a read-only container. |
| Full Caddy module comparison | Rebuilt and upstream module lists match under the Compose capability policy. |
| Workflow resolver | Both image/context resolver commands passed using the checkout interpreter on PATH. |
| Native container browser journey | Passed with fictional Organization `phase4-browser-20261031170531`; token-bearing request URLs: zero. |
| amd64 container browser journey | Passed with fictional Organization `phase4-browser-20261031171439`; token-bearing request URLs: zero. |
| Migration replay | Both runs reported schema version 1 with no newly applied migration. |
| Same-architecture recreation | The Organization count and fictional artifact hash survived; the marker was removed afterward. Readiness returned HTTP 200 and an oversized account form returned HTTP 413. |
| Supply-chain export | Both maximum-mode provenance and SPDX files were exported, extracted, and parsed as JSON. Final packaging evidence is retained alongside the scan outputs. |
| Python quality | Passed Ruff, Basedpyright, compilation, and whitespace checks before the final repository gate. |

## Shared repository guardrail

The closing complete repository gate passed: zero Ruff and Basedpyright
diagnostics, all 88 architecture tests, documentation checks, and
code-documentation checks. The final log is
`/tmp/impodo-patched-repository-final.log`.

Earlier checks failed while the concurrent documentation work was in progress.
The other session resolved the Production code/test links and the screenshot
ownership and missing-image issues. This session added the remaining registered
`BusinessIntentReview` module link to the Final review developer page without
changing its workflow text. Standalone documentation checks and the complete
gate then passed. No failed guardrail was waived.

The new storage baseline belongs to the concurrent workstream. Therefore the
trial used fresh databases and secrets instead of attempting to migrate the
existing rehearsal database. Architecture-specific PostgreSQL volumes were
kept separate for the qualifying trials. No physical data-directory transfer
between architectures is qualified by this report.

The trial is isolated under Compose project
`impodo-patched-image-rehearsal`, with only loopback proxy port 8444 exposed.
Its configuration and fictional secrets are below the ignored
`deploy/.local/patched-image-rehearsal/` directory. The separate destructive
contract suite uses only `impodo_image_test` on loopback port 54339. The
existing rehearsal's database and artifact volumes were preserved.

Local probes were corrected before qualification: the generated Compose
configuration initially omitted the disabled admin profile; the resolver
probe needed the checkout interpreter on PATH; and the upstream Caddy module
comparison needed the same capability configured by Compose. These were
test-setup corrections, not security-policy exceptions.

## Limits and next release checks

Docker Desktop supplied native Linux arm64 containers and emulated Linux
amd64 containers. Hosted Ubuntu execution of the replacement workflow remains
unrun. Clean Debian and Ubuntu host qualification, unfixed-vulnerability and
license acceptance, published immutable output digests, production secrets,
remote certificates, backup and restore, privileged MFA enforcement, and
sole-operator recovery remain open.

This session performed no branch creation, commit, merge, push, external
message, or remote deployment. The complete repository gate must pass before
the repository is handed off as ready.
