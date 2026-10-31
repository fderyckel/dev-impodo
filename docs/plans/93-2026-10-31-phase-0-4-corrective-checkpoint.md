---
audience: developer
kind: report
status: current
---

# Shared-server Phases 0 to 4 corrective checkpoint

## Decision supported by this report

The corrective pass addresses the account, secret-isolation, concurrency,
fixture, and CI gaps recorded in the
[Phase 0 to 4 recheck](92-2026-10-31-phase-0-4-recheck.md). The local container
trial remains usable. Shared deployment remains blocked by supporting-image
vulnerability findings and the existing qualification and security decisions.
This report does not authorize Phase 5 or a remote deployment.

Work stayed on `main`. No branch, commit, merge, or push was created. Another
session continued editing matching, company selection, joins, and related-data
review in the same checkout. Its edits were preserved. Results below identify
the scope they actually verified rather than certifying an actively changing
tree.

## Implemented corrections

| Finding | Result |
| --- | --- |
| Bootstrap password visible to web | The one-time bootstrap process has a separate secret volume. Web and worker receive the runtime DSN and identifier key. Provisioning removes the password left in older runtime volumes. A real Linux-container test runs provisioning twice and checks non-root access. |
| Unbounded public password work | Login, activation, reset consumption, and reset requests share a process-local admission gate: two concurrent operations, a global burst of 60 and source burst of 30 per refill window of 60 seconds, and at most 1,024 retained source digests. Busy reset requests retain their generic acknowledgement. Other busy forms return HTTP 429. |
| Hashing never-issued tokens and trying two reset services | A short PostgreSQL read checks the purpose-bound token proof, issued state, and expiry, then dispatches to the correct principal service before hashing. Consumption still atomically rechecks all eligibility, revision, expiry, and single-use conditions. No database lock is retained during password hashing. |
| Missing request and container bounds | Proxy and application reject request bodies larger than 65,536 bytes. Application checks cover declared and streamed bodies. The local web container has a 1 GiB memory limit, two CPUs, and a 128-PID limit. These are trial limits, not measured production capacity. |
| Current-session revocation always stale | The sessions page uses **Sign out this browser** for the current session and the existing logout route. Other-session revocation retains its expected-revision predicate. |
| Malformed anonymous CSRF cookie causes HTTP 500 | Both the cookie and submitted token must be bounded ASCII before constant-time comparison. Malformed cookies now produce the generic HTTP 400 response. |
| Different users serialize on their Organization row | Session checks and Organization Administrator authorization retain exclusive user and assignment locks but take a shared Organization lock. Concurrent readers can proceed; suspension remains blocked until those reads finish and denies later session access. |
| Broken behavioral and performance fixtures | Fixtures now use current composition ports, request identities, schema versions, portable identity bindings, and explicit business expectations. The dependency benchmark constructs a real assessment and retains its original semantic and call-count assertions. Required and optional derived-reference cases are distinct. Historical migration fixtures reconstruct the historical columns instead of making production migrations permissive. |
| Incomplete CI coverage | Python quality now requires complete portable behavior matrices on Linux, macOS, and Windows, with default performance tests on Linux. Linux tests retain disposable PostgreSQL ownership. A supporting-image matrix collects every critical and high alert and fails on fixable alerts at those severities. |

Test artifacts use operating-system temporary directories in the corrected
fixtures. The mapping page's support details moved unchanged into a focused
partial to satisfy the existing template size guardrail. Workflow ownership
entries were reconciled without changing the active workstream's behavior.
The causal-quality test now checks all retained causal edges, as required by
the existing evaluator-9 contract. Matching presenter fixtures use the real
workspace state required by the shared company partial.
The BoM composition fixture submits its observed parent-plan version when
previewing another join, preserving the current stale-plan protection and
the separate header, line, and operation grain assertions.
Four later schema-fixture failures were missing `odoo_version` attributes
after the related-data work began using that existing schema field. The
corrected live fixtures specify Odoo 19; the manual fixture specifies no live
version. Their original availability and preservation assertions remain.

## Verification results

| Check | Observed result |
| --- | --- |
| Focused portable identity, PostgreSQL adapter, account route, and transport contracts | 58 passed; 26 subtests passed. |
| Linux-owned suite with disposable PostgreSQL and actual Docker secret provisioning | 28 passed. |
| Default performance contracts | 59 passed; 28 subtests passed; 9 explicit opt-in cases skipped. |
| Causal-quality and matching presenter follow-up | 31 passed; one explicit opt-in case skipped. |
| BoM source-composition follow-up | Passed with separate header, line, and operation grains and no Odoo connection or schema calls. |
| Full Project setup browser journey after fixture repair | Passed, including its subtest. Explicit business expectations and assessment evidence precede loading. |
| Committed demo scenarios | Passed, including all four real PLW manufacturing files, the offline contact canary, profile round trip, fixture-integrity checks, and replay protections. No live Odoo target was used. |
| Larger fictional workflows | Passed: 150 lists reused, 4,753 people loaded, and 4,903 records reconciled; 100,000-row source composition and 65,299-row composition with Recipe reuse also passed. These are functional checks, not production capacity qualification. |
| Complete portable suite in the changing checkout | Failed: 182 failures, 2,350 passes, 37 skips, and 1,215 passing subtests. Most failures report a source-build mismatch between the parent and newly spawned workers. A fresh isolated load-workflow rerun passed all seven tests. This failed run is not classified as passing. |
| Stable source-copy portable rerun | 2,530 passed, 38 skipped, and 1,261 passing subtests in 38 minutes 41 seconds. The command failed on four fixture cases: causal-edge selection, two matching presenter namespaces, and the missing BoM parent-plan version. |
| Corrected fixture and admission follow-up in a fresh source copy | 35 passed; one explicit large-scale opt-in case skipped. This rerun covers all four failed cases and the new admission tests. |
| Latest derived-table navigation and layout | All 10 test bodies passed with the installed Edge browser. A temporary runner selected Edge instead of downloading Chromium; repository test bodies were unchanged. |
| Late related-data review changes | 52 passed; 32 subtests passed across the affected domain, application, and browser contracts. |
| Schema governance, presenter, and capture follow-up | Initially four missing-version fixture errors; after the fixture corrections, all 19 tests and two subtests passed. |
| Complete repository gate | Passed: zero Ruff or Basedpyright diagnostics, all 85 architecture tests, documentation checks, and code-documentation checks. |
| Actual rebuilt container browser journey | Passed with fictional Organization `phase4-browser-20261031150049`; no token-bearing request URLs were observed. |
| Running-image correspondence | All 61 selected identity, PostgreSQL, and server-composition Python files matched the checkout. This does not certify every concurrent local-workflow edit. |
| Persistence and migration replay | Organization count and a synthetic artifact hash survived container recreation. Two migration runs reported schema version 12 with no migration applied. |
| Actual HTTPS negative probes | Never-issued token: HTTP 400; malformed anonymous cookie: HTTP 400; oversized form: HTTP 413; readiness: HTTP 200. |

The rebuilt local image is `linux/arm64`, image ID
`sha256:ea3c71a774e2a8256085d181a050ae84f482e9a645705556d9bc9c22f56a1945`.
These runtime checks do not qualify a different CPU architecture.

The stable source copy is an ordinary temporary filesystem snapshot with the
existing `main` metadata and the checkout's locked environment. It creates no
Git branch or merge. Its application sources are loaded through an explicit
`PYTHONPATH`. Freezing the source avoids weakening the worker build contract
while the other session continues editing. Follow-up fixes made after the
copy require their own focused verification.

The default environment has no Playwright installation, so the complete
portable command skipped optional real-browser checks. The explicit Edge
follow-up above supplies the derived-table navigation evidence. Other skipped
cases and hosted browser execution were not converted into passes. The
complete portable invocation itself remains a failed historical run; the
35-test follow-up records the repaired cases separately.

Related-data review changes arrived during the complete run. They received a
new source-copy capture and the 52-test and 19-test follow-ups above. The
application files in that capture still matched the checkout when those
checks finished. This comparison does not prevent subsequent edits by the
other session.

The initial standalone documentation check failed while the other session's
new related-table screenshot was absent. That failure remains historical
evidence; the complete gate below determines the closing documentation status.

Local logs are temporary verification aids:

- `/tmp/impodo-corrective-identity-final.log`
- `/tmp/impodo-corrective-linux.log`
- `/tmp/impodo-corrective-performance.log`
- `/tmp/impodo-corrective-causal-presenter.log`
- `/tmp/impodo-corrective-portable.log`
- `/tmp/impodo-corrective-snapshot-tests.log`
- `/tmp/impodo-corrective-frozen-followup.log`
- `/tmp/impodo-corrective-navigation-browser.log`
- `/tmp/impodo-corrective-supporting-followup.log`
- `/tmp/impodo-corrective-schema-followup-final.log`
- `/tmp/impodo-corrective-gate-final.log`
- `/tmp/impodo-corrective-browser.log`

## Supporting images still block acceptance

The Compose candidates retain the same PostgreSQL and Caddy versions but use
refreshed official image digests:

- `postgres:18.6-bookworm@sha256:5c4783a8cc3f0074912164f94dfa1be4e043438f0f707cf968d7af9a0d31a2c8`
- `caddy:2.11.7-alpine@sha256:e70b164da81ef698b3ad19af799ea6086ce6d9e48ed4079eb9fe26f6fbf14ee5`

The actual running versions were checked after recreation. Pinned Trivy
0.76.1 collected all critical and high alerts and separately ran the exact
fixable-alert rejection policy used in CI. Counts are package alerts, not
distinct vulnerabilities or proven exploits.

| Image | Critical alerts | High alerts | Alerts with a fixed version | Policy result |
| --- | ---: | ---: | ---: | --- |
| Rebuilt Impodo application | Not recollected in this pass | Not recollected in this pass | 0 | Passed the fixable-alert gate. Earlier full-alert triage remains open. |
| Refreshed PostgreSQL | 4 | 94 | 26 | Failed; all 26 fixable alerts are in the bundled `gosu` Go binary. |
| Refreshed Caddy | 1 | 9 | 9 | Failed; three fixable package alerts and six fixable Go-binary alerts remain. |

Refreshing PostgreSQL removed the earlier fixable Debian package findings.
It did not make the image acceptable. No vulnerability suppression, policy
waiver, or license acceptance was added. The new hosted supporting-image jobs
will reject these candidates until upstream remediation or an explicit,
documented acceptance decision resolves the findings.

Full supporting-image results are in
`/tmp/impodo-corrective-postgres-scan.json` and
`/tmp/impodo-corrective-caddy-scan.json`. The exact gate logs are
`/tmp/impodo-corrective-app-gate.log`,
`/tmp/impodo-corrective-postgres-gate.log`, and
`/tmp/impodo-corrective-caddy-gate.log`.

## Remaining limits

Hosted Linux, macOS, and Windows CI execution, Debian and Ubuntu host
qualification, supporting-image and license acceptance, production secrets,
remote certificates, backup and restore, privileged WebAuthn enrollment and
enforcement, and sole-operator account recovery remain open. Isolating the
bootstrap password does not qualify a break-glass recovery procedure or rotate
the fictional operator's current password. Local email delivery remains
disabled. No live Odoo write target or remote server was used.

The live tests prove that different users can share the Organization read
lock and that suspension still excludes subsequent access. They do not
measure realistic concurrent login throughput or justify adding a connection
pool. Connections remain short-lived and PostgreSQL-only; no DuckDB owner or
cross-request connection cache was introduced by these account changes.

The continuation decision remains local trial only under the
[implementation plan](07-linux-server-postgresql-duckdb-implementation.md).
