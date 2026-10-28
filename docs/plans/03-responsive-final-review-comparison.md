---
audience: developer
kind: plan
status: current
---

# Finish responsive Final review in local and shared-server profiles

## Status, reader, and decision

**Status:** Re-baselined against the checkout on 2026-10-28. This revision
replaces the earlier single-store recovery proposal in this file. Background
comparison, atomic DuckDB report publication, compact navigation facts,
operation-aware liveness, and one-pass snapshot totals are current. A durable
local comparison attempt, a compact **Check changes** page, measured
publication qualification, and the later shared-server job integration remain
open.

This plan is for maintainers who will finish Final review without merging the
local and shared-server persistence models. It remains the third item in the
[cross-plan implementation order](00-remaining-work.md).

The data-manager outcome is unchanged: a long read-only comparison can survive
an application restart, the browser can reopen the same result, and ordinary
review pages do not load the complete execution schedule merely to show totals
and the first bounded groups.

The deployment decision has changed since the original proposal:

- The supported local product keeps its control state, comparison evidence,
  and bounded projections in local DuckDB stores. It may continue to execute a
  comparison in one process-local worker.
- The future shared server uses PostgreSQL for Organization-scoped operations,
  jobs, attempts, leases, idempotency, progress, and bounded page projections.
  It continues to use DuckDB and immutable artifacts for detailed migration
  evidence and comparison output.
- PostgreSQL does not replace DuckDB as the comparison engine or detailed
  evidence store. Local Impodo does not acquire a PostgreSQL dependency.

The current server application exposes health and migration-control
infrastructure only. It does not yet expose the migration browser, accounts, a
durable worker, or Final review. The server part of this plan therefore remains
gated by the shared-server Phase 4, 5, and 6 prerequisites. Local work may
proceed without claiming that a remote Final review is available.

## Audit of the original proposal

The following findings explain why the implementation sequence and storage
ownership need revision.

| Earlier assumption | Current evidence | Revised decision |
| --- | --- | --- |
| One recoverable attempt receipt in the Workspace database can serve every installation. | Local comparison control is in `PreflightJobManager`, while the accepted server architecture assigns operations, jobs, leases, and progress to PostgreSQL. The Workspace engine already remains the authority for detailed preflight evidence. | Define one deployment-neutral attempt contract. Persist local attempt control in the local DuckDB registry. Persist future server attempt control in PostgreSQL. Keep the completed comparison and its artifacts in the Workspace data plane in both profiles. |
| Reusing the active Workspace job is sufficient idempotency. | The local manager returns any active job for the Workspace and stores no semantic operation identity. A changed request could therefore be treated as the same attempt, and a restart loses the attempt. | Bind every submission to an operation ID and a semantic input hash. Reuse only the same operation and meaning. Reject changed meaning under the same identity, and never run two comparison writers for one Workspace. |
| The main remaining page problem is navigation. | `ExecutionPreviewSummary` already supplies bounded navigation facts. However, `ExecutionService.current_preview` still materializes the full `ExecutionSnapshot` for **Check changes**, and the execution router shares one full-preview renderer across review, confirmation, and outcome states. | Keep navigation as implemented. Add a separate hash-bound review projection and a dedicated **Check changes** view model. Continue to materialize and validate the complete snapshot at confirmation, load submission, execution, recovery, and reconciliation boundaries. |
| Moving comparison into a background thread is the complete database-lifetime fix. | The worker protects event-loop liveness during classification, but the comparison POST still performs synchronous local repository work before enqueueing. Several Final review and load routes use generic thread-pool calls instead of one operation-sized database scope. | Move each local route's complete synchronous read or command into one `run_page_read` or `run_local_operation` callback. Split local evidence loading, remote Odoo I/O, and local publication so no retained DuckDB owner survives across the remote call. |
| Publication optimization can be designed from the current scale harness. | The harness records useful application phases, memory, artifact sizes, and database size. It does not yet retain repeated raw runs, CPU time, DuckDB connection and schema counts, lock retries, or concurrent health latency. It also creates its temporary root below the repository `.tmp` directory. | Repair the measurement harness first. Use operating-system temporary directories and measure the current implementation before selecting a storage change. |
| A successful DuckDB commit is enough to finish a future server job. | The shared-server architecture now requires an idempotent PostgreSQL operation, a fenced Workspace lease, an immutable DuckDB data-plane receipt, and a verified PostgreSQL projection receipt. | Make the comparison publication capable of producing a value-free data-plane receipt. The server worker must project that exact receipt after commit and must never hold a PostgreSQL transaction while reading Odoo or writing DuckDB. |

The original safety invariants, the compact-review goal, and the requirement to
measure before adopting a bulk insertion path remain valid.

## Non-negotiable behavior

Every increment in this plan must preserve these rules:

- Final review remains read-only with respect to Odoo.
- One eligible prepared row produces exactly one persisted comparison
  decision in canonical order.
- The manifest remains authoritative for classifications, differences,
  issues, and portable review meaning.
- The manifest and execution snapshot remain immutable and hash-bound to the
  exact source, mapping, preparation, target, read identity, and dependency
  order.
- A new comparison becomes current only when the complete DuckDB publication
  commits. A partial attempt cannot replace the previous current report.
- Artifacts without a matching committed comparison or data-plane receipt do
  not prove success.
- A compact browser projection is display-only. It cannot authorize a load or
  replace the full execution snapshot.
- Load confirmation and submission reload the complete snapshot and validate
  its submitted hash, current target, current read identity, current write
  identity, authorization, and execution journal state before Odoo changes.
- Job records, progress, receipts, projections, diagnostics, and logs contain
  no credential, source value, target business value, numeric Odoo identifier,
  or complete row collection.
- Odoo reads remain grouped by model and bounded domain. No connector call is
  added inside a source-row loop.
- A local route retains database owners for one bounded synchronous operation
  only. It releases them before an `await`, remote Odoo call, background job,
  redirect, or unrelated action.
- Neither profile introduces a process-wide or cross-request DuckDB connection
  cache.
- Server workers prove the current fencing token before committing detailed
  evidence and before publishing its receipt. Local execution does not pretend
  to provide a PostgreSQL lease.

## Target control and evidence model

### Shared application contract

Introduce a value-free comparison request and attempt projection at the
application boundary. The exact class names may change, but the contract must
carry the following meaning:

- The request names the operation, Project, Workspace, actor, and requested
  time.
- Its semantic input hash covers the current Workspace revision, mapping
  revision and hash, staging hash, quality hash, normalization revision and
  hash, eligible-dataset hash, target hash, and read-credential binding or
  generation available without materializing every prepared row.
- The data-plane receipt adds the derived frozen-input hash, requirement-plan
  hash, report result hash, manifest hash, execution-snapshot hash, artifact
  identities, and committed preflight run ID.
- The attempt exposes only bounded phases, timestamps, terminal state, safe
  failure code, and result receipt identity.
- A completed comparison that contains blockers is a successful read-only
  computation with a review-required outcome. It is not an infrastructure
  failure.

The port must not accept a Python callback, API key, DuckDB connection,
filesystem path, or PostgreSQL session as durable input. The local composition
may adapt the request to an in-process worker. A future server worker resolves
the immutable manifest and secret reference through its own composition.

### Local profile ownership

The local DuckDB registry owns value-free attempt discovery and progress. The
existing `project_operation_intent` seam should be extended or wrapped instead
of creating a second unrelated local job catalogue. It records the operation
identity, Workspace owner, semantic input hash, actor, current phase, terminal
state, safe failure code, and committed result identity.

The Workspace engine remains authoritative for the completed readiness report,
decision rows, target snapshots, execution projection, and current pointer.
The immutable artifact store remains authoritative for the manifest and
execution snapshot. The committed Workspace evidence must retain the operation
identity so startup recovery can distinguish a committed comparison from an
unrelated current report.

Local startup reconciliation follows these rules:

1. If an unfinished intent names a matching committed current report and the
   report's immutable artifacts pass their hashes, mark the attempt successful
   and return its result.
2. If no matching committed report exists, mark the old attempt interrupted.
   Keep the previous current report unchanged and permit a new read-only
   comparison.
3. If artifacts exist without matching committed Workspace evidence, leave
   them unreachable for bounded owner-qualified cleanup. Do not infer success.
4. If the Workspace bindings changed, mark the attempt stale and send the data
   manager to the owning earlier stage.

The local progress route reads the attempt projection. It must not materialize
the manifest, execution snapshot, or target snapshots merely to display a
phase.

### Shared-server profile ownership

The server integration must use the accepted cross-store protocol rather than
copy the local registry design into a Workspace DuckDB file:

1. The authenticated route fixes the Organization and Project scope, verifies
   the capability, and reserves one PostgreSQL operation and durable job for
   the semantic input.
2. The worker claim creates an attempt and acquires the Workspace lease with a
   monotonic fencing token. PostgreSQL then closes the claim transaction.
3. The worker materializes only owner-qualified inputs. It opens DuckDB for one
   bounded local phase, releases it, performs the approved Odoo reads without a
   PostgreSQL transaction or retained DuckDB owner, and then opens one bounded
   fenced publication phase.
4. The Workspace transaction publishes the comparison and an immutable
   data-plane receipt together. The receipt binds the operation, semantic
   input, fencing token, evidence hashes, and artifact identities.
5. PostgreSQL verifies that exact receipt, advances the bounded Final review
   and load-review projections, records the job result, and emits a projection
   receipt in one short transaction.
6. The progress and status pages read PostgreSQL only. They do not open every
   Workspace DuckDB database.

If the worker stops before a data-plane receipt exists, recovery records an
interrupted attempt and may allow a fresh read-only attempt. If the receipt
exists but the PostgreSQL projection is missing, recovery verifies and replays
the projection without running the comparison again. A stale fencing token or
receipt mismatch fails closed. An ambiguous Odoo write rule is not relevant to
comparison because Final review never writes to Odoo, but its output still
cannot authorize a later load without the normal full validation.

This server slice cannot start until the shared-server plan supplies accounts,
request identity, scoped authorization, the durable job schema, worker
runtime, secret references, Workspace leases, and the first earlier job-family
qualifications. It remains after preparation, source intake, and Odoo capture
in the Phase 6 migration order.

## Work package 1 - Establish a trustworthy baseline

Measure the current local paths before refactoring them:

- `GET /workspaces/{workspace_id}/summary` in an action-rich state;
- the local portion of `POST .../summary/compare` before enqueue;
- the background phases for frozen-input loading, requirement planning, Odoo
  reading, classification, snapshot construction, artifact publication, and
  DuckDB publication;
- the progress HTML and status JSON routes while comparison is active; and
- `GET .../load/review` with a representative multi-dataset execution
  snapshot.

Record elapsed time, CPU time, peak RSS, physical DuckDB connection count,
connection time, schema-check count and time, lock retries and wait time,
artifact sizes, database size, connector request counts, and concurrent
authenticated health and status latency. Retain raw repeated fresh-process
runs as well as the summary.

Update the opt-in scale harness to use an operating-system temporary directory.
Do not require the repository-local `.tmp` directory. Keep the fixture
fictional and delete source bytes before the child process when the test is
proving prepared-evidence independence.

This work package changes no comparison meaning and sets no wall-clock
continuous-integration threshold. Connection, schema, query-shape, hash, row
count, and connector-call assertions provide the deterministic regression
gate.

## Work package 2 - Bound local route and worker database lifetimes

Refactor the local start path into explicit phases:

1. One `run_local_operation` callback resolves the request actor and Workspace
   lineage, verifies authorization, loads the bounded current evidence
   bindings, resolves the read-credential binding, reserves or reuses the
   operation, and enqueues no work while a retained database owner is active.
2. After the callback returns, the route submits the already-bound attempt to
   the local runtime and redirects to its progress page.
3. The worker loads and verifies the complete frozen input inside one bounded
   synchronous local scope, then releases every DuckDB owner.
4. The worker performs the approved Odoo reads with no retained local database
   owner.
5. The worker classifies and publishes the result inside one bounded local
   operation. Existing repository transactions remain independent until the
   publication repository opens its one atomic transaction.

Do not keep an operation scope open merely to reduce a later connection count.
Remote I/O is a hard boundary. If the split makes a large immutable input cross
phases in memory, measure that memory explicitly and keep it bounded by the
accepted row limits.

Add route-level tests that prove the start callback and each local page render
run off the event loop on one worker. Derive connection ceilings from the
actual registry, Workspace reference, Workspace engine, and protected-store
owners used by the fixture. Do not copy a ceiling from another route.

## Work package 3 - Add durable local comparison attempts

Add `PREFLIGHT_COMPARE` or an equivalently specific operation kind to the local
restart-safe operation seam. Extend the route form or command request with a
canonical operation ID. The first submission reserves the intent. An exact
replay returns the same active or terminal attempt. Reuse of that identity for
different semantic input fails closed.

Persist phase changes at coarse boundaries only. Progress writes must not
compete with decision-row insertion or contain customer values. A restart may
lose sub-phase timing, but it must retain the attempt identity and honest
terminal recovery state.

Bind the operation ID into the committed preflight evidence without changing
the canonical manifest, comparison classifications, decision order, or
execution-snapshot semantic hash. Recovery verifies the current report and
artifact hashes before it marks the intent committed. The comparison
publication remains responsible for keeping the previous current pointer
unchanged on failure.

Do not add a local PostgreSQL service, server lease, Organization dashboard, or
remote scheduler to finish this work package.

## Work package 4 - Make **Check changes** use a compact review projection

Extend the existing hash-bound execution projection with only the information
the pre-load review page needs:

- per-dataset display label, target model, and create, update, and unchanged
  counts;
- total writes and deferred relationship counts;
- at most five load-order groups with a count of omitted groups;
- at most five blocker groups with safe owning actions and a count of omitted
  groups;
- current execution and reconciliation status needed to choose the next page;
  and
- the current snapshot hash, root hash, preflight run, target binding, read
  identity bindings, projection contract version, and execution-shape
  readiness.

The projection contains no source row, business identity, field value,
numeric Odoo identifier, credential, authorization object, or write payload.
Navigation queries continue to select only their scalar columns; they do not
deserialize the detailed review groups.

Separate the execution router's view models by responsibility:

- The load landing route uses bounded navigation state to choose Final review,
  **Check changes**, or the saved outcome.
- `GET .../load/review` uses the compact review projection inside one
  `run_page_read` callback and does not call
  `current_execution_snapshot`.
- `GET .../load/confirm` reloads and validates the complete immutable snapshot
  because it presents the exact confirmation.
- Load submission, execution, recovery, reconciliation, and historical outcome
  detail continue to use the complete snapshot and their existing
  authorization and stale-evidence checks.

Historical current reports without a compatible compact projection must fail
closed with one explicit **Compare with Odoo again** action. Do not silently
rebuild a new projection from changed evidence during a page read.

## Work package 5 - Optimize publication only from measurements

Add privacy-safe timings inside the publication owner for:

- manifest and execution-snapshot serialization;
- each immutable artifact write;
- dataset-row encoding and insertion;
- decision-row encoding and insertion;
- target-snapshot encoding and insertion;
- compact execution projection insertion;
- committed-operation binding or data-plane receipt insertion;
- current-pointer update; and
- local registry or server PostgreSQL projection finalization.

First reduce duplicate reads, repeated serialization, and avoidable schema
checks inside the bounded operation. Preserve authorization, stale-evidence
checks, and transaction independence.

If repeated raw runs prove that row-oriented decision insertion is the
dominant publication cost, test a bounded DuckDB bulk path such as a registered
Polars batch followed by `INSERT ... SELECT`. Adopt it only when the same
fixture demonstrates a repeatable gain and the implementation:

- preserves canonical decision order and byte-equivalent decision JSON;
- limits batch memory under the accepted row ceilings;
- verifies the inserted row count before the current pointer advances;
- keeps report rows, target snapshots, execution projection, operation
  binding, transitions, audit evidence, and current pointer in one DuckDB
  transaction; and
- rolls the whole publication back on any failure.

Never commit independent decision batches. Never treat a lower connection
count or one fixture run as a latency claim.

## Work package 6 - Integrate Final review with the shared server

Start this work only after the shared-server Phase 6 prerequisites and earlier
job families pass their gates. Implement the deployment-neutral attempt port
with PostgreSQL jobs and the existing cross-store coordination contracts.

The server route must authorize before storage access, reserve an
Organization-scoped idempotency key, and store only a secret reference and
generation. The worker must recheck authority when it starts, resolve only the
leased Workspace, prove its fence before data commit and receipt publication,
and close its PostgreSQL transactions before Odoo or DuckDB work.

Project the ready or review-required result into PostgreSQL from the exact
data-plane receipt. The projection may contain bounded counts, readiness,
target-binding hash, artifact identities, and current receipt hash. It must
not contain comparison rows, target snapshots, the execution schedule,
protected values, credentials, or complete report payloads.

Qualify restart before and after the DuckDB commit, artifact publication,
data-plane receipt, PostgreSQL projection, and progress update. Prove that a
stale worker cannot become current and that another Organization cannot list,
poll, download, or recover the attempt by guessing its IDs.

This work package updates the current server architecture and workflow
documentation only when the migration browser and worker behavior actually
ship. Until then, this plan is the sole documentation owner for the proposal.

## Delivery order

Implement the work in this order:

1. Repair the measurement harness and capture the current local baseline.
2. Introduce the value-free request and attempt contract while bounding local
   route and worker database lifetimes.
3. Persist and recover local comparison attempts through the local DuckDB
   control plane.
4. Publish and consume the compact **Check changes** projection.
5. Adopt a DuckDB publication change only if the expanded evidence proves a
   repeatable bottleneck and improvement.
6. Integrate the same application contract with PostgreSQL jobs and fenced
   workers only when the shared-server phase dependencies are complete.

Each step must leave the local product usable. The remote step cannot delay a
safe local recovery or review improvement, and local implementation must not
create a second server architecture.

## Acceptance checks

### Shared behavior

- A successful 25,000-row comparison retains the same manifest semantic hash,
  execution-snapshot semantic hash and root hash, classifications, decision
  order, target snapshots, and connector request shape as the reference path.
- Final review remains unable to construct a write-capable Odoo adapter.
- Duplicate operation submission never starts overlapping comparison work.
- Changed meaning under one operation identity is rejected.
- A failure before current-pointer commit leaves the previous current report
  and load review unchanged.
- Artifacts alone never produce a successful attempt.
- The summary, progress, status, navigation, and **Check changes** routes do not
  materialize the complete execution snapshot.
- Confirmation and load submission do materialize and validate the complete
  snapshot and reject a stale submitted hash before a journal or writer is
  created.
- Odoo connector calls remain bounded by the requirement plan and do not grow
  once per source row.
- Logs, attempts, receipts, progress, and diagnostics remain value-free.

### Local profile

- The comparison POST completes its synchronous local work in one
  `run_local_operation` callback and redirects without waiting for Odoo reads,
  classification, artifact creation, or publication.
- Summary, progress, status, and **Check changes** remain responsive during the
  scale run, and authenticated health does not show a false disconnection
  while status responses are arriving.
- Reopening a progress page after an application restart shows the recovered
  successful or interrupted attempt instead of treating every missing
  in-memory job as an unknown 404.
- An interruption before publication preserves the previous result and permits
  a new comparison. An interruption after the matching commit recovers the
  successful result.
- Focused request tests enforce evidence-based physical-connection and schema-
  check ceilings. The shared database-owner lifecycle tests remain green.
- The local application starts and completes this workflow without PostgreSQL,
  Organization administration, a server worker, or a remote scheduler.

### Shared-server profile

- The Final review HTTP surface remains unavailable until authenticated
  request identity, scoped authorization, durable jobs, secret references,
  leases, and the worker runtime are assembled.
- Progress and result discovery use bounded PostgreSQL queries and open no
  Workspace DuckDB database.
- PostgreSQL transactions do not span Odoo reads, artifact writes, DuckDB
  work, or user wait time.
- A worker with an expired or superseded fencing token cannot commit or publish
  a current receipt.
- A committed data-plane receipt with a missing PostgreSQL projection is
  projected without rerunning comparison or Odoo reads.
- Organization and Project scope are present in every operation, job, lease,
  receipt, projection, status, and artifact lookup.
- PostgreSQL contains no source rows, prepared rows, target records, protected
  identifiers, execution rows, credentials, or full manifest.

### Performance qualification

- Repeated fresh-process local runs retain raw measurements for elapsed time,
  CPU, peak RSS, database and artifact sizes, application phases, DuckDB
  connections, schema checks, retries, lock wait, connector calls, and
  concurrent health and status latency.
- Local Windows release evidence and the available macOS development evidence
  use checkout-local tools and operating-system temporary directories. They
  require no administrator rights, `/Applications` installation, Accessibility
  permission, Screen Recording permission, or system-wide browser changes.
- Linux server measurements are added only after the server worker can execute
  this job family on a qualified host. A local Compose control-plane trial is
  not presented as server workload evidence.
- A bulk publication path, if adopted, retains one atomic DuckDB commit and
  shows a repeatable improvement on the same fixture without increasing
  connector calls or changing evidence.

## Ownership and verification

Comparison meaning remains in `application/preflight_service.py` and the pure
preflight and execution-snapshot domain packages. The deployment-neutral
attempt contract belongs below `application`. The local runtime belongs in the
local composition and DuckDB registry adapter. The future server runtime
belongs in PostgreSQL job and coordination adapters plus the server worker
composition. Neither adapter becomes visible to the domain service.

Detailed publication remains owned by
`adapters/duckdb/preflight_repository.py`. Compact review meaning belongs with
`application/workspace/execution/navigation.py` or a focused sibling module.
The browser routes own only transport validation, phase boundaries, and view
selection.

Extend the focused preflight service, job, repository, navigation, execution,
diagnostic, route, scale, interruption, and server coordination suites. Add
failure injection before and after operation reservation, frozen-input load,
Odoo read, each artifact publication, DuckDB commit, local intent completion,
server receipt projection, and lease expiry.

Run the focused behavioral tests, Python quality gate for every Python change,
documentation checks for every documentation change, and the complete
repository gate before delivery. Update the paired user and developer pages,
preflight contract, workflow registry, code map, and authenticated screenshots
only when each described behavior is implemented and verified.

This plan does not change classification, reviewed deferred groups, execution
idempotency, reconciliation, correction, Odoo compatibility, or Production
authority.
