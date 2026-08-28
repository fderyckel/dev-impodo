# Impodo remaining work

## Status and authority

**Status:** Active roadmap, updated 2026-08-25.

This is the broad forward-looking roadmap. An approved detailed delivery plan
may live beside it while that work remains unfinished. The current detailed
plans are [Recipe runs in three pages](recipe-run-three-page-ui-refactor.md)
and the proposed [scalable relationship dependency
planner](scalable-relationship-dependency-planning.md).
Completed behavior belongs in architecture, contracts, user and developer
documentation. Point-in-time implementation evidence belongs in
`docs/reports/`, `docs/testing/`, and Git history.

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

The [scalable relationship dependency
plan](scalable-relationship-dependency-planning.md) defines the proposed
execution-planning, cycle, recovery, request-count, and BOM qualification work.
Its proposed status does not activate this deferred track or raise a current
row limit.

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

This plan does not authorize implementation while the track remains Deferred.
Product ownership must activate the track before the first delivery increment
begins.

Before activation, verify the implemented Odoo-source browser boundary and
reconcile its current documentation. The developer workflow and focused
browser tests currently cover mapping, offline preparation, and read-only
comparison, while the current user source page and BPMN still describe capture
as the stopping point. Correct that current-status disagreement without
presenting the planned write capability as available.

#### Goal and first supported case

A data manager can capture bounded records from a disposable Odoo 19 database,
prepare approved scalar corrections, review the current differences, confirm
one update-only operation, and verify the resulting Odoo state. Impodo changes
only the originally captured records in the same configured database.

For example, a data manager may capture 40 fictional customer records from a
resettable rehearsal database. Final review may show that 12 records are ready
to update and 28 already match. Impodo must update only those 12 original
records, verify all 40 outcomes, and show no proposed update when the data
manager compares the same prepared result again.

The first supported case includes only the Tier-1 scalar field types already
accepted by `CURRENT_ODOO_SOURCE_POLICY`. It excludes creates, deletes,
relationship writes, computed or related fields, translated fields,
company-dependent fields, arbitrary Odoo methods, and business workflow
actions. Unsupported types, models, fields, and side effects remain
fail-closed.

#### Ownership and lifecycle

The disposable round trip extends the existing Project-owned Authoring lineage:

```text
MigrationProject
|-- one frozen Authoring DataVersion
|   `-- Odoo source snapshot and protected capture origins
`-- one Authoring MigrationRun
    |-- one immutable binding to the same disposable Odoo target
    |-- one execution attempt for each reviewed comparison
    `-- one Authoring MigrationWorkspace
        `-- mapping, preparation, comparison, and detailed outcome evidence
```

The objects keep these responsibilities:

- The Project remains the business and governance root. The round trip does
  not create another Project.
- The Authoring DataVersion owns the accepted Odoo source snapshot and its
  protected origins. Execution never changes or replaces that frozen source
  evidence.
- The Authoring `MigrationRun` owns the target binding and the execution
  lifecycle. A target I/O `ExecutionRun` is one attempt inside that
  `MigrationRun`; it is not another Test or Production `MigrationRun`.
- The Authoring workspace selects the DataVersion dataset and contains the
  current mapping, preparation, and comparison work. Detailed execution and
  reconciliation artifacts may use the isolated workspace and protected
  Project stores, but their storage location does not change their run
  ownership.
- A Recipe remains portable reusable meaning. An `odoo_pinned_update` mapping
  cannot be published as a Recipe, and this track does not create a
  `RecipeApplication`.
- An Integrated Test run, CutoverPlan, rollout selection, and Production run
  do not participate in this capability. `PRODUCTION_WRITE_UNSUPPORTED`
  remains the executable policy.

After verified completion, Impodo keeps the frozen DataVersion and evidence
unchanged and completes the Authoring run. A later correction starts a
successor DataVersion, Authoring run, and workspace with a fresh Odoo capture.
It does not reopen the completed workspace or reuse its write authority.

#### Non-negotiable boundaries

- Source and destination must have the same exact connection-target hash and
  the same protected record identity. Impodo provides no business-key, create,
  cross-database, or missing-record fallback.
- The operator must confirm that the target is disposable and resettable. The
  confirmation binds the actor, Authoring run, target, and reviewed execution
  plan. It grants no authority to another comparison or run.
- A separate write-role credential must pass the exact target, principal,
  company context, readable-model, and writable-model probes. Read access or a
  Project editing capability never implies write access.
- Final review remains read-only. It produces the protected input for a later
  explicit **Confirm and load** action but does not itself authorize a write.
- Numeric Odoo IDs, principal and company identifiers, protected filters, and
  target-bound values remain `RESTRICTED_TARGET_EVIDENCE`. Impodo encrypts them
  before persistence and excludes them from portable mappings, snapshots,
  reports, downloads, and ordinary journal projections.
- The writer uses only the Odoo 19 ORM-backed JSON-2 surface and the exact
  reviewed model, record IDs, and fields. It exposes no generic client,
  direct SQL, `sudo`, caller-selected method, import fallback, or browser
  automation.
- Impodo records planned attempts before the first write. A lost or invalid
  write response becomes `OUTCOME_UNKNOWN`, stops all later writes, and cannot
  be retried before reconciliation.
- Native JSON-2 still cannot make the final compare and write atomic. A
  just-in-time re-read narrows the race but does not remove it. This accepted
  limitation is permitted only for an explicitly disposable target.

#### Delivery increment 1: establish executable ownership and contracts

Define the immutable contracts before enabling any writer:

1. Extend the run-owned target model so an Authoring run can seal the exact
   Odoo target used by its accepted source capture. Do not use a mutable
   workspace projection or the DataVersion source binding as write authority.
2. Add a protected pinned-update execution plan that binds the Project,
   DataVersion, `MigrationRun`, workspace, source-policy hash, capture manifest,
   mapping, compiled plan, prepared evidence, comparison run, target binding,
   approved fields, and canonical row root.
3. Bind every target I/O `ExecutionRun` and reconciliation result to the
   enclosing Authoring `MigrationRun`. Bounded registry status may summarize
   the attempt, while the exact protected rows remain outside the registry.
4. Add a purpose-specific execute capability for guarded Odoo-source updates.
   Do not infer it from read, Project-edit, Recipe, Test, or Production
   capabilities.
5. Version every changed immutable payload. Add a forward storage upgrade only
   for mutable structural metadata; never reinterpret or rewrite an existing
   hash-bound capture, comparison, journal, or result.

This increment exits when contract and repository tests reject every wrong
Project, DataVersion, run, workspace, target, and identifier namespace before
opening protected storage or contacting Odoo.

#### Delivery increment 2: freeze the protected update plan

When Final review produces a current `READY` Odoo-source comparison, Impodo
derives a protected executable plan from only its `UPDATE` rows:

1. The plan carries the original protected numeric ID, source-row trace,
   expected current value, proposed value, field type, and comparison evidence
   for each approved field.
2. The portable review carries only bounded counts, non-sensitive row tokens,
   status, and hashes. It contains no numeric ID or protected business value.
3. `UNCHANGED` rows remain accounting evidence and produce no write intent.
   Any missing record, schema change, absent baseline, or concurrent approved-
   field change blocks the complete plan.
4. A zero-update comparison completes without constructing a writer or asking
   for a write credential.
5. Any change to source, capture, schema, mapping, approved fields,
   preparation, comparison, target, or policy makes the plan stale.

This increment exits when deterministic serialization, corruption,
substitution, stale-binding, portable-redaction, and protected-store tests pass
without making an Odoo write call.

#### Delivery increment 3: authorize and recheck one disposable load

The **Check changes** and **Confirm and load** steps establish fresh authority:

1. Impodo displays the exact Authoring run, disposable target, database,
   approved field scope, update count, unchanged count, and needs-refresh
   count.
2. The data manager explicitly confirms that the database is disposable and
   confirms the current protected plan once. An identical submission reuses
   the same attempt; changed meaning under the same operation identity fails
   closed.
3. Impodo resolves only the target-bound write-role credential and probes the
   exact reviewed read-back and write scope. Credential re-entry appears only
   for a missing, rejected, or insufficient credential.
4. Impodo creates the run journal and all planned row attempts before the first
   write.
5. Immediately before writing, Impodo re-reads every candidate by protected ID
   in bounded model-and-field batches. If any record is missing, inaccessible,
   changed in an approved field, or incompatible with the captured schema,
   Impodo records that no write was attempted, stops the whole load, and sends
   the data manager back to **Final review**.

This increment exits when wrong-target, stale-review, changed-credential,
Production-purpose, repeated-submit, and concurrent-change tests all prove
zero writer calls.

#### Delivery increment 4: execute the Tier-1 scalar updates

The first writer slice updates only the protected ID and approved scalar
fields from the current plan:

1. The writer calls the standard Odoo 19 ORM `write` operation through the
   closed JSON-2 adapter. It never searches for an update record by business
   key and never sends a create or delete operation.
2. Each record receives one separately journaled update. A definitive Odoo
   rejection records `FAILED`. A lost response records `OUTCOME_UNKNOWN` and
   stops all later writes.
3. Background progress uses the authorized job snapshot and bounded status
   projections. It must not reopen a worker-held workspace DuckDB database or
   read the registry once per row.
4. The local disposable Odoo path is qualified first. The same contracts must
   remain transport-neutral so remote support does not add a second semantic
   implementation.

Exact-ID prechecks and read-back must be batched by model and field scope.
Single-record writes are an intentional first safety boundary, but their call
count and latency must be measured. The implementation must not add a
per-record schema read, identity lookup, permission probe, repository query,
or relationship lookup around those writes.

This increment exits when a live disposable local Odoo 19 run records every
success, definitive rejection, and unknown outcome without a duplicate or
unplanned write.

#### Delivery increment 5: reconcile and close the Authoring run

Impodo verifies the actual database before it reports completion:

1. Reconciliation reads journaled protected IDs in bounded model-and-field
   batches. It never falls back to a business key or assumes that a missing
   record proves an uncertain update was not applied.
2. The result records `VERIFIED`, `DIFFERENT`, `MISSING`, `NOT_WRITTEN`, or
   `OUTCOME_UNKNOWN` without editing the execution journal.
3. An uncertain update is never marked retry-safe. The data manager must
   resolve its outcome before another update plan can be created.
4. A fully verified result completes the enclosing Authoring `MigrationRun`.
   Fallout or an unresolved outcome leaves that run `INCOMPLETE` and retains
   one obvious recovery action.
5. Impodo runs a fresh read-only comparison after successful reconciliation.
   The prepared result must classify as `UNCHANGED`; a new `UPDATE` or
   `BLOCKED` result fails the idempotency exit gate.

This increment exits when success, partial rejection, lost response, missing
record, changed read-back value, manual recovery, and repeat-comparison tests
produce complete and deterministic evidence.

#### Delivery increment 6: qualify the remote disposable path

Remote enablement reuses the same domain and evidence contracts. It adds only
the approved HTTPS transport, separate remote read and write credentials,
target-bound identity probes, and remote failure handling. It must reject
redirects, context drift, credential-role fallback, and a target change before
the writer is constructed.

An opt-in sanitized remote Odoo 19 acceptance run must exercise connection
loss before and after a write, timeout, Odoo rejection, credential rotation,
record-rule denial, company-context mismatch, and repeat reconciliation. The
acceptance report must record exact request counts and latency. Remote support
does not change the disposable classification or grant Production authority.

#### Qualification after the first round trip

Each additional field type, model class, relationship, or known Odoo side
effect requires its own evidence for serialization, baseline comparison,
write behavior, constraints and automation, read-back, idempotence, and
bounded access. Extend the allowlist only after that evidence passes. Do not
generalize from one standard model to custom models, or from scalar fields to
relationships and business actions.

Batching changes require parity tests for ordered intents, row-level journal
outcomes, unknown-response handling, and reconciliation. A performance change
must not combine several rows into one outcome that Impodo cannot later prove.

#### Acceptance and documentation gate

The first disposable-target exit gate requires all of the following evidence:

- The full browser path presents **Check changes**, **Confirm and load**, and
  **Verify result** with one obvious next action and an explicit
  non-production warning.
- Domain, repository, service, router, worker, presenter, and browser tests
  prove the complete Project, DataVersion, `MigrationRun`, workspace, and
  target lineage.
- Tests prove that Recipe publication remains blocked for
  `odoo_pinned_update`, no `RecipeApplication` or CutoverPlan is created or
  changed, and Production cannot construct a writer even through a direct
  service call.
- Protected-evidence tests prove encryption, authenticated bindings, private
  storage, retained-history limits, expiry, project deletion, and the absence
  of IDs and values from portable artifacts and ordinary projections.
- Call-count tests prove fixed identity and schema probes, batched exact-ID
  comparison and read-back, and no hidden N+1 lookup around the intentional
  per-record writes.
- A resettable local Odoo 19 database completes one sanitized successful run
  with updates and unchanged rows. A second comparison proves the expected
  unchanged result. Separate injected scenarios cover stale rows, definitive
  failures, and an unknown response.
- The remote acceptance gate passes separately before the browser describes
  remote Odoo-source loading as available.
- Current user and developer workflow pages, `docs/workflow.yml`, contracts,
  architecture, ADR consequences, the Python code map, BPMN, screenshots, and
  acceptance documentation are updated together only as each behavior becomes
  current. Planned behavior must not appear in current user instructions.

Focused verification must cover `tests/integration/odoo/test_provenance.py`,
`tests/application/workspace/review/test_odoo_comparison.py`, the new pinned execution and reconciliation
contract tests, `tests/application/workspace/execution/test_service.py`,
`tests/application/workspace/execution/test_reconciliation.py`, `tests/integration/web/test_execution.py`,
`tests/architecture/test_canonical_ownership.py`, `tests/integration/protected_evidence/test_project_security.py`, and the
focused browser tests in `tests/integration/web/test_review_workflow.py` and
`tests/integration/web/test_load_workflow.py`. The normal documentation
quality and repository gates remain required.

Production remains a later architecture decision. It requires strong
target-instance identity and one server-side atomic lock, compare, and write
operation, plus restore and race tests, ACL and record-rule coverage, privacy
and threat review, fault injection, backup and rollback evidence, and measured
batch and call counts. Until those guarantees exist,
`PRODUCTION_WRITE_UNSUPPORTED` remains unchanged.

### 5. Add governed corrections to a completed load

**Status:** Proposed and deferred. The workflow is not implemented.

The [completed-load correction plan](completed-load-correction-workflow.md)
defines a successor Authoring run that reuses one unchanged file-source
DataVersion, preserves the completed load, recalculates evidence automatically,
compares previous intent with current Odoo state and corrected intent, and
writes only confirmed field differences to exact protected target records.

Product ownership must activate this track and its disposable-target boundary
before implementation. Integrated Test and Production correction remain
outside the first delivery.

### 6. Conditional target-side gateway

**Status:** Deferred.

A signed Odoo 19 add-on, manifest-bound grants, target-side receipts, and named
business-action handlers may be reconsidered only when a proven execution
requirement cannot be met safely through the existing bounded connector and
executor contracts. No generic RPC, SQL, `sudo`, or caller-selected method
surface is permitted.

### 7. Conditional hosted composition

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
