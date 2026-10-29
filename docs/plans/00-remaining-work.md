# Impodo remaining work

## Status and authority

**Status:** Active roadmap, reconciled against the checkout on 2026-10-28.

This is the broad forward-looking roadmap. Detailed plans remain beside it
while delivery or qualification is unfinished. The [documentation index](../README.md#plans)
lists those plans and their remaining scope.

Completed behavior belongs in architecture, contracts, and user and developer
workflow documentation. Point-in-time evidence belongs in an owning plan or
Git history. Remove completed delivery detail and retire completed plans after
those current references have been verified.

## How the implementation order is decided

The primary criterion is the improvement to a data manager's work. A change
ranks higher when it helps more migrations, removes repeated or technical
decisions, enables a useful migration path, prevents a wrong write, or makes
interrupted work recoverable. Safety and accepted product boundaries remain
mandatory. Dependencies and implementation difficulty may change the delivery
sequence between similarly valuable changes, but they do not move indirect
engineering work above a stronger operator outcome.

This is the only cross-plan implementation order. A detailed plan owns its
internal sequence and prerequisites, but its older Priority 1, 2, or 3 label
does not override this list.

## Ordered implementation work

1. **Finish the [six-stage Authoring support](01-six-stage-workflow-user-support.md).**
   This remains the most important change because every data manager benefits
   when Impodo explains a blocker, names the affected business records, opens
   the owning correction, and states which evidence remains safe. Finish the
   root-cause correction routes first. Add migration intent and the load
   cockpit after the safe technical scope is known.
2. **Complete [hierarchy-root and grouped-cause qualification](04-hierarchy-root-null-scope-and-load-order.md).**
   This remains important for categories, bills of materials, and other parent
   structures. It lets the data manager correct one missing or ambiguous
   parent instead of reviewing thousands of repeated downstream consequences.
3. **Broaden the [Odoo-to-Odoo migration](05-odoo-to-odoo-migration.md).**
   This remains a high-value feature. Extend the qualified Contact path to
   Product, bills of materials, empty destinations, interruption, and recovery
   after the hierarchy qualification is reliable.
4. **Add [exact-record repair for known load fallout](06-load-fallout-source-cell-workbook.md).**
   This remains important even though it is not the normal path. When a load
   partly fails, repairing only the reviewed target records can save a data
   manager from rebuilding and repeating the complete migration. Product
   ownership must activate the writer because uncertain outcomes and
   concurrent target edits require strict safeguards.
5. **Continue the [shared-server implementation](07-linux-server-postgresql-duckdb-implementation.md).**
   A shared server remains strategically important for collaboration, durable
   work, central access, and returning to long-running results. It no longer
   ranks first merely because backend work has started. Continue bounded local
   persistence without displacing the higher direct workflow improvements,
   and do not claim a shared release before the supported-host gate passes.
6. **Complete the [end-to-end trial and scenario lifecycle](08-end-to-end-trial-and-scenario-qualification.md).**
   This remains important for confidence, training, recovery, and repeatable
   Product and bill-of-material evidence. Its developer regression value is
   substantial, but the normal data-manager workflow above should improve
   before more scheduling and scenario infrastructure is added.
7. **Qualify [workflow responsiveness and recovery across platforms](09-responsive-workflow-liveness-and-navigation.md).**
   This remains important because slow or apparently frozen pages undermine
   confidence. Measure the implemented path first and add a cache or process
   split only when the retained evidence shows a user-visible need.
8. **Finish [macOS Match data save qualification](10-resilient-match-data-saving.md).**
    This remains important for data managers who use macOS, especially for
    uncertain saves, stale tabs, and restart recovery. The behavior is already
    implemented, so this item closes supported-platform evidence rather than
    adding another workflow.
9. **Finish the bounded [preparation correctness and measurement work](11-preparation-efficiency-and-storage.md).**
    This remains important where large or error-heavy datasets block current
    work. Complete consumer boundaries, bounded finding publication, browser
    recovery, and reproducible measurements. Keep the separate 100,000-row
    mixed and related expansion deferred until real migration demand justifies
    it.
10. **Close [Match data ordering qualification](12-smart-match-data-ordering.md).**
    The implemented ordering is useful, but the remaining work is primarily
    documentation, visual acceptance, and measurement. Do not redesign the
    algorithm without evidence of a data-manager problem.
11. **Close [row-inclusion visual qualification](13-source-row-inclusion-rules.md).**
    Guided inclusion is already available and useful. Finish the authenticated
    examples and documentation. Native execution remains conditional unless
    the bounded fallback becomes a measured release blocker.
12. **Activate the remaining [Odoo 20 Production promotion](14-odoo-19-and-20-support.md)
    only when product demand requires it.** Ordinary Odoo 19 and Odoo 20 work
    is already supported. The remaining Production matrix is important for an
    organization that needs that exact release path, but it should not outrank
    improvements that help every current migration.
13. **Continue [execution-service maintainability](15-maintainable-code-and-execution-services.md)
    alongside the product work it protects.** This remains important because
    unclear ownership makes safe changes slower and riskier. It is supporting
    work rather than a standalone data-manager feature, so extract the focused
    boundary needed by the next product change instead of running a broad
    refactor first.

Each plan retains its safety gates. This order does not authorize an external
write, live customer-data exercise, deferred Production operation, or remote
deployment.

## Files that are not separate implementation items

- [Linux server deployment with PostgreSQL control and DuckDB workers](07-vm-deployment-duckdb-worker-management.md)
  remains the target architecture for item 5. Do not implement it as a second
  server track. Keep it aligned with the detailed plan, then retire duplicated
  delivery detail when current architecture documentation owns the decisions.
- This file, `00-remaining-work.md`, is the roadmap and ordering authority. It is
  not an implementation item.
- The [shared-server Phase 0 baseline](90-2026-10-22-shared-server-phase-0-baseline.md)
  remains useful historical evidence. It requires no implementation.
- The [local empty-server container rehearsal](91-2026-10-27-local-empty-server-container-rehearsal.md)
  remains useful current qualification evidence until supported-host evidence
  supersedes it. Its open findings belong to item 5 rather than a separate
  implementation stream.

## Local execution constraint

The current macOS development account has no administrator rights. Work on
this laptop must use the repository-local runner, `.venv`, `.uv-cache`,
operating-system temporary directories, authenticated test applications, and
headless browser or HTTP-level checks that need no macOS Accessibility or
Screen Recording permission. Do not make `/Applications`, system-wide browser
settings, privileged services, or security-policy changes a prerequisite.

Clean Linux-host, firewall, certificate, service-manager, and attached-storage
qualification belongs on an approved external host or continuous-integration
runner. Record it as omitted when that environment is unavailable; do not ask
the local user to weaken or change laptop security settings.

## Completed plans removed in this review

The 2026-10-28 review removed the completed source-table composition plan and
the completed three-page Production journey plan. Current source composition
belongs to the Source data workflow and its focused tests. Current Production
behavior belongs to the Production rollout guide, workflow registry, and run
tests.

The Stage 1 and Stage 2 page-loading plan was also retired after its implemented
batches were verified in the current code and workflow documentation. Its only
open platform measurements already belong to the shared responsiveness plan.

## Current implemented boundary

The [architecture overview](../architecture/overview.md) owns the current
Project, Data version, Recipe, run, and workspace model. The
[Prepare data developer workflow](../developer/workflow/04-prepare-data.md#columnar-compilation-and-execution)
owns the implemented compiler and native Polars execution boundary.

The current preparation limits are:

- 100,000 physical rows for exact-snapshot, single-dataset direct mappings
  compiled entirely to the verified native-columnar route;
- 50,000 physical rows for current direct Python-fallback or relationship
  routes; and
- 50,000 physical rows for one low-cardinality single-column lookup-derived
  table without advanced checks or reference data; and
- 25,000 physical rows for other derived or materialized routes.

This roadmap does not raise, remove, or reinterpret those limits.

## Long-term deferred tracks

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

Historical Windows relationship qualification in Git retains the completed
current-boundary evidence. That result does not activate this deferred
high-volume track or raise a row limit.

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

Git history retains the completed measurements and failed qualification
evidence. Transport or hash-root changes remain conditional on measured benefit
and must not weaken artifact verification.

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
- The writer uses only the supported Odoo 19 or final Odoo 20 ORM-backed
  JSON-2 surface and the exact
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

### 5. Broaden completed-load corrections beyond the Authoring boundary

**Status:** Deferred.

The current Authoring workflow can correct scalar fields and exact-existing
many-to-one fields after a verified Odoo load. It preserves the completed load,
recalculates evidence in a successor workspace, and writes only confirmed
field differences to exact protected target records. The
[Load into Odoo developer workflow](../developer/workflow/06-load-into-odoo.md)
defines that current boundary. Git history retains its measured local and
remote qualification evidence.

Identity-field corrections, supporting-record creation, Odoo-source
corrections, and corrections inside Integrated Test or Production runs each
require a separate proposal and qualification. None is inferred from the
current Authoring result.

### 6. Add exact-record repair for known load fallout

**Status:** Deferred. The current workbook and append-only re-verification
behavior remains available. The proposed exact-ID writer in
[the detailed repair plan](06-load-fallout-source-cell-workbook.md) is not
scheduled and must not be inferred from the existing successor correction
workflow.

### 7. Promote Odoo 20 Production

**Status:** Deferred. Final Odoo 20 supports the qualified ordinary read,
write, recovery, and same-major Recipe boundaries. Production remains blocked
until its separate deployment, browser, recovery, Integrated Test, transfer,
and representative-model promotion gate is activated and passed. The
[dual-version support plan](14-odoo-19-and-20-support.md) owns that gate.

### 8. Conditional target-side gateway

**Status:** Deferred.

A signed Odoo 19 add-on, manifest-bound grants, target-side receipts, and named
business-action handlers may be reconsidered only when a proven execution
requirement cannot be met safely through the existing bounded connector and
executor contracts. No generic RPC, SQL, `sudo`, or caller-selected method
surface is permitted.

### 9. Native row-inclusion execution

**Status:** Deferred unless measurement makes the Python fallback a release
blocker. The implemented evaluator remains bounded and authoritative. A native
route must prove exact parity before it can change capability selection or row
limits.

The former “conditional hosted composition” track is no longer deferred. The
shared-server architecture is approved and retains bounded local trial work as
item 5 in the current product-value order. Corporate identity providers remain
sequenced after the first server release; they are not a prerequisite for the
current container or PostgreSQL phases.

## Explicitly excluded work

The current plans do not include cross-major Odoo transfers, automatic Recipe
conversion between Odoo majors, whole-database Odoo upgrades, unrestricted
recursive Odoo graph capture, or a generic RPC, SQL, `sudo`, or caller-selected
method surface. These are deliberate exclusions, not unprioritized backlog.

## Selecting the next track

No deferred track becomes active automatically. Product ownership must record
the next priority in this file, state its accepted prerequisites, and define
how it affects Project, workspace, Recipe, DataVersion, run, and cutover
evidence before implementation begins.
