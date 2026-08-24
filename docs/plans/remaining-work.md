# Impodo remaining work

## Status and authority

**Status:** Active roadmap, updated 2026-08-24.

This is the only forward-looking plan in `docs/plans/`. It records genuinely
unfinished or explicitly deferred product work. Completed behavior belongs in
architecture, contracts, user and developer documentation. Point-in-time
implementation evidence belongs in `docs/reports/`, `docs/testing/`, and Git
history.

Finishing an item means removing its delivery detail from this file after the
current documentation and evidence have been updated. Completed plan files do
not remain in this directory as an archive.

## Current implemented boundary

Impodo supports a Project-first local or remote Odoo 19 workflow. A Project may
contain no Recipe or several Project-scoped Recipes. The Project owns complete
DataVersion source packages, Authoring, Test, and Production runs, workspaces,
and its CutoverPlan. A Recipe owns immutable reusable rule revisions.

The current preparation limits are:

- 100,000 physical rows for exact-snapshot, single-dataset direct mappings
  compiled entirely to the verified native-columnar route;
- 50,000 physical rows for current direct Python-fallback or relationship
  routes; and
- 25,000 physical rows for current derived or materialized routes.

This roadmap does not raise, remove, or reinterpret those limits.

Recognized older versions of the current Project registry, DataVersion,
MigrationWorkspace reference, and workspace-engine generations upgrade
forward transactionally before use. Retired generations and newer unknown
versions remain fail-closed. This implemented release boundary is not a
deferred roadmap item.

## Deferred tracks

Deferred work is recorded here so it remains visible, but it is not authorized
for implementation merely because the active gate completes. Maintenance,
security, data-loss prevention, dependency compatibility, and regressions that
threaten the current workflow remain in scope.

### 1. Qualify related and mixed preparation at 100,000 rows

**Status:** Deferred. The direct-route foundation is implemented; the mixed and
derived high-volume route is not qualified.

The unfinished outcome is a bounded Product/BOM workflow for approximately
16,000 products and 80,000 BOM lines, plus a representative mixed or derived
100,000-row fixture. The work must complete set-based derived and grouped
production, logical projection, relationship accounting, and dependency
propagation without whole-run Python collections.

Before raising any limit, the release evidence must prove:

- identical ordered values, lineage, issues, effects, control totals, and
  hashes across batch sizes;
- zero Odoo calls during transformation and no query, scan, or Python callback
  per BOM line;
- explicit unique, missing, ambiguous, duplicate, unsafe-parent, and resolved
  relationship states;
- crash, cancellation, and retry safety that preserves the previous current
  run; and
- three fresh Windows worker runs below the accepted time and memory limits,
  including a reproducible improvement for the sanitized 1,000-customer case.

The [transformation scale implementation
log](../reports/transformation-scale-implementation-log.md) retains the
completed measurements and failed qualification evidence. Transport or
hash-root changes remain conditional on measured benefit and must not weaken
artifact verification.

### 2. Add optional clean-package certification

**Status:** Deferred.

Formal organization-specific certification remains a possible future track.
Current Test qualification and fresh Production evidence do not constitute a
general clean-package certificate and do not reuse Test approval as Production
approval.

### 3. Complete general remote acceptance and production readiness

**Status:** Deferred except for the implemented remote Test-to-Production
workflow.

Broader production matrices, representative-customer rollout programs,
organization assurance levels, and business actions remain unfinished. The
[remote Odoo acceptance
runbook](../developer/runbooks/remote-odoo-acceptance.md) continues to govern
existing opt-in acceptance behavior.

### 4. Complete guarded Odoo-source updates

**Status:** Deferred. Bounded Odoo-source capture, immutable local publication,
offline preparation, and read-only three-way comparison are implemented.

The remaining increments are:

1. **Disposable guarded updates:** derive an exact update scope from reviewed
   evidence, probe a separate write principal, re-read concurrency evidence,
   update only protected numeric IDs, journal every attempt, stop on unknown
   outcomes, read back, reconcile, and prove repeat comparison is idempotent.
2. **Type, model, relationship, and side-effect qualification:** qualify each
   additional field or model class for serialization, baseline comparison,
   write behavior, read-back, side effects, idempotence, and batched non-N+1
   access. Unsupported classes remain fail-closed.
3. **Production authorization:** support Production writes only if a strong
   target-instance identity and a narrow atomic lock/check/write seam can be
   proven without direct SQL, generic remote methods, or caller-selected
   operations. Otherwise Production Odoo-source writes remain unsupported.

The first exit gate is a fully journaled and reconciled disposable Odoo 19
round trip that is clearly labelled as non-production. Production additionally
requires restore and race tests, ACL and record-rule coverage, privacy and
threat review, fault injection, backup and rollback evidence, and measured
batch/call counts.

### 5. Conditional target-side gateway

**Status:** Deferred.

A signed Odoo 19 add-on, manifest-bound grants, target-side receipts, and named
business-action handlers may be reconsidered only when a proven execution
requirement cannot be met safely through the existing bounded connector and
executor contracts. No generic RPC, SQL, `sudo`, or caller-selected method
surface is permitted.

### 6. Conditional hosted composition

**Status:** Deferred.

PostgreSQL repositories, object storage, durable workers, distributed target
locks, SSO actors, centralized authorization, and managed secrets remain
conditional on an approved hosted deployment requirement. The local
composition remains authoritative until then.

## Selecting the next track

No deferred track becomes active automatically. Product ownership must record
the next priority in this file, state its accepted prerequisites, and define
how it affects Project, workspace, Recipe, DataVersion, run, and cutover
evidence before implementation begins.
