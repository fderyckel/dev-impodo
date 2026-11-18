# Qualify workflow responsiveness across platforms

## Status and remaining scope

The [roadmap](00-remaining-work.md) ranks this work fifth. The 25,000-row
macOS fixture passed the Overview and polling budgets. A matching Windows
run and a permitted loopback run of the launcher and restart tests remain
open. The [platform testing reference](../developer/reference/platform-support-and-testing.md)
describes the fixture, reproduction command and manual workflow input.

The [setup developer workflow](../developer/workflow/00-project-setup.md) owns
navigation reads, and [Match data diagnostics](../developer/workflow/03-match-data.md#operational-diagnostics-and-responsiveness)
owns the browser recovery and local diagnostic behavior. Completed design and
implementation records remain in Git history.

Transfer Overview now reads a fixed-size summary. The populated upgrade
regression covers zero and 25,000 exclusions for Odoo 19 and 20 in local and
remote modes. Comparison tests check schema validation separately during
preparation, collection of inputs for the Odoo read, and publication. Browser
tests cover a minute of progress, connection loss, recovery of uncertain
writes and the end of polling when a session expires.

## Remaining measurement work

Run the same fixture on Windows using the measured macOS application build,
source bytes, mapping and pinned runtime. If the application build has
changed, run both platforms on the new build. Retain both reports.

Measure cold and warm Overview, Match data, Final review, and Check changes.
Retain p95 latency, CPU time, working-set growth, registry and workspace-engine
connections, schema checks, artifact bytes read, thread-pool queue delay,
owner reads, navigation reads, and template time. Verify that each child emits
normal diagnostic records or a bounded diagnostic-unavailable reason.

The macOS lane must run from the checkout without administrator rights. Use
the repository-local runner, `.uv-cache`, operating-system temporary
directories, authenticated test clients, and a checkout-launched headless
browser when browser timing or focus evidence is required. The lane must not
depend on macOS Accessibility or Screen Recording permission, `/Applications`,
or a system-wide browser change. Use the existing cross-platform continuous-
integration lane for a clean hosted macOS or Windows environment when the
local account cannot provide one.

The proposed acceptance budgets remain:

| Operation | Proposed p95 budget |
| --- | --- |
| Warm Overview | 1.5 seconds |
| Cold Overview | 3 seconds |
| Authenticated health and active-job status during page construction | 250 milliseconds, with every request below one second |

Treat these as qualification targets until repeated platform runs establish
release evidence. Do not report an unmeasured target as an achieved guarantee.

### Temporary Windows diagnostic kit

Extract the supplied kit to a short, writable local folder and double-click
**Run-Windows-Check.cmd**. The first run may download the pinned Python
runtime and its packages. Keep the laptop awake and return the
`impodo-windows-diagnostic-*.zip` beside the launcher. This separate
collection uses fictional data and temporary projects.

The ZIP records the eight launcher and restart checks and the Windows event-loop check in
`windows-qualification.json`. Page measurements are in
`workflow-liveness.json` once collection reaches that stage. Check coverage
and the application, fixture and runtime identities against the retained
macOS samples before accepting a Windows pass. A setup interruption can
produce a ZIP with the last recorded stage. Record the outcome and missing
evidence in the private performance history.

After reviewing the returned ZIP and finishing any required rerun, retire the
[temporary launcher](../../scripts/Run-Windows-Check.cmd), its
[collector](../../scripts/windows_workflow_diagnostic.py), the distributed
kit and this subsection. The four-page benchmark, ordinary diagnostic export
and retained evidence have separate ongoing uses.

## Conditional follow-up

- Measure the bounded uncached navigation query first. Add a cross-request
  cache only if evidence justifies it. Bind any cache to the complete current
  evidence and job generation, with stale-binding, job-transition, and restart
  tests before enabling it.
- Measure comparison, load confirmation, execution, and reconciliation
  separately. Move credential-free CPU work to a bounded process only when the
  measurements show a need. The current
  [Final review developer workflow](../developer/workflow/05-final-review.md)
  owns the implemented local boundaries. The
  [shared-server plan](07-linux-server-postgresql-duckdb-implementation.md)
  owns the future PostgreSQL job and worker migration.
- The [Match data save qualification](10-resilient-match-data-saving.md)
  owns concurrent editor saves and the live restart exercise.

## Regression and completion gates

Overview must perform bounded work independent of record count, avoid full
execution and comparison artifacts, and use at most one checked registry and
one checked workspace-engine connection for owner and navigation facts.

Exercise a 60-second slow but progressing operation, server loss, HTTP 401,
429, 500, and 503, and an uncertain mutation. Authenticated responses must keep
connectivity distinct from operation success; uncertain mutations retain their
identity and resolve through receipt read-back without automatic repetition.
Keep business values, credentials, tokens, formulas, and raw URLs out of logs.

Retain the raw platform evidence and record accepted budgets in testing and
current developer documentation. Remove this plan when the measurements and
any justified repairs pass; a cache is not a required deliverable.
