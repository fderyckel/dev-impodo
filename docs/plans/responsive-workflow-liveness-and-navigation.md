# Qualify workflow responsiveness across platforms

## Status and remaining scope

**Status:** Bounded shared navigation, operation-aware browser recovery, and
process diagnostics are implemented. Representative platform p95 qualification
and measurement-driven follow-up remain open as of 2026-09-15.

The [setup developer workflow](../developer/workflow/00-project-setup.md) owns
navigation reads, and [Match data diagnostics](../developer/workflow/03-match-data.md#operational-diagnostics-and-responsiveness)
owns the browser recovery and local diagnostic behavior. Completed design and
implementation records remain in Git history.

The current navigation query uses compact evidence and keeps full snapshot
validation at load authorization and execution. Qualification must preserve
that boundary and the separate operation and connectivity states.

## Remaining measurement work

Use a deterministic mature workspace with at least 25,000 prepared records and
a large immutable execution snapshot. Run it in fresh processes on Windows
and macOS while probing authenticated health and active-job status.

Measure cold and warm Overview, Match data, Final review, and Check changes.
Retain p95 latency, CPU time, working-set growth, registry and workspace-engine
connections, schema checks, artifact bytes read, thread-pool queue delay,
owner reads, navigation reads, and template time. Verify that each child emits
normal diagnostic records or a bounded diagnostic-unavailable reason.

The proposed acceptance budgets remain:

| Operation | Proposed p95 budget |
| --- | --- |
| Warm Overview | 1.5 seconds |
| Cold Overview | 3 seconds |
| Authenticated health and active-job status during page construction | 250 milliseconds, with every request below one second |

Treat these as qualification targets until repeated platform runs establish
release evidence. Do not report an unmeasured target as an achieved guarantee.

## Conditional follow-up

- Measure the bounded uncached navigation query first. Add a cross-request
  cache only if evidence justifies it. Bind any cache to the complete current
  evidence and job generation, with stale-binding, job-transition, and restart
  tests before enabling it.
- Measure comparison, load confirmation, execution, and reconciliation
  separately. Move credential-free CPU work to a bounded process only when the
  measurements show a need. The [Final review follow-up](responsive-final-review-comparison.md)
  owns remaining comparison receipt, preview, and publication work.
- Repeat the [macOS save qualification](resilient-match-data-saving.md),
  including concurrent catalogue readers from separate editor identities.

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
