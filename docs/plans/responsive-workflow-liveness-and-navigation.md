# Distinguish a busy Impodo from a disconnected Impodo

## Status and decision

**Status:** Core repair implemented on 2026-09-14. The original evidence came
from a read-only inspection of a running Product migration; that inspection
changed no running request, workspace evidence, or Odoo state. Representative
platform p95 qualification remains a release-measurement follow-up rather than
an architectural dependency.

Replace the single browser state **Impodo is not responding** with separate
operation and connectivity state machines, and remove full execution-snapshot
loading from shared workflow navigation. A pending browser navigation is not
proof that the server is healthy, but a few missed two-second health checks are
also not proof that the process is disconnected.

The immediate performance repair is to give shared project navigation a
bounded application read model. Opening Overview or another ordinary workflow
page must not materialize row-level comparison evidence. The read model is not
a second source of workflow truth: it derives its facts from current evidence
pointers and compact summaries that belong to the stage which published them.
The full immutable execution snapshot remains required at the load
authorization and execution boundaries.

The intended reader is the developer repairing cross-workflow responsiveness
and browser recovery without weakening saved-operation, comparison, or load
safety.

## Implementation outcome

- `WorkspaceNavigationQueryService` now supplies an immutable scalar read
  model from one workspace-engine transaction. The presenter maps those facts
  and does not call workflow services.
- File-source comparison publishes one `ExecutionPreviewSummary` in the same
  transaction as the readiness evidence. Navigation does not open the
  execution snapshot, comparison manifest, row decisions, target snapshot, or
  normalization dry-run document.
- Existing workspaces without the new projection fail closed in shared
  navigation until a new comparison publishes it. Load review and execution
  still validate the complete immutable snapshot.
- Overview performs its complete owner, navigation, and template build in the
  bounded thread pool. Canonical owner and optional target-setup records share
  one registry connection; source packages remain in their separate
  DataVersion store and are not part of the common owner view.
- The browser tracks connectivity separately from `BUSY`, `DELAYED`, and
  `OUTCOME_UNKNOWN` operation state. Known work becomes amber after 20 seconds;
  red disconnection requires 45 seconds without an authenticated response and
  a newer timeout or network failure.
- Job-status responses update liveness. A 401 ends the session; 429, 500, and
  503 prove connectivity without being classified as successful work.
- Server children fall back to a bounded process-local diagnostic JSONL file
  when the shared log cannot open. Bundles include those allowlisted records,
  and common page rendering reports owner, navigation, template, DuckDB, and
  queue timings.

## Evidence behind the proposal

The inspected Product workspace exposed a specific cross-cutting hot path:

- The running web-server child was alive and actively computing. One two-second
  sample consumed 1.94 CPU seconds; its working set was about 423 MiB.
- The workspace-engine database was approximately 283 MB.
- The current immutable execution snapshot was approximately 36 MB and its
  comparison manifest was approximately 8 MB.
- `workspace_setup.py::workspace_overview` performs synchronous repository and
  rendering work directly in an asynchronous route.
- `_render` obtains a complete owner view and builds shared workflow navigation
  for nearly every workspace page.
- For a mature file-source workspace, `build_workspace_navigation` performs a
  sequence of source, schema, mapping, preparation, review, and load reads.
- Its load-stage calculation calls `ExecutionService.current_preview`. That
  method loads and verifies the complete execution snapshot, reads the
  manifest, scans snapshot rows, and builds browser summaries. This work is
  unnecessary for an Overview breadcrumb or stage status.
- Repository reads commonly open a DuckDB connection and run a workspace
  schema check. Repeating them through the navigation builder amplifies the
  cost.
- The browser heartbeat runs every four seconds, gives `/health` two seconds to
  answer, and shows the red disconnected banner after three failures. A
  synchronous or CPU-heavy page build can therefore starve `/health` and be
  misclassified as a stopped server.

The current diagnostic file for this process contains launcher lifecycle
records but no application-start, request-completion, or event-loop-delay
records. That observability gap prevents exact attribution of the current page
time and must be repaired before performance budgets are finalized.

These observations explain why the browser tab can show an active loading
indicator at the same time as Impodo shows **Impodo is not responding**. The
browser knows that a navigation is pending; the old page's health request is
not being scheduled or answered quickly enough. Neither signal alone proves
whether useful work is progressing.

## Required safety boundaries

- Do not automatically repeat a save, comparison, load, or other mutation.
- A timeout remains **outcome unknown** until its operation receipt or durable
  evidence proves the outcome.
- Do not restart the server merely because a foreground request is slow.
- Keep the health route authenticated, same-origin, and independent of a
  workspace database or Odoo.
- A compact navigation read model or review projection is display-only. It
  cannot authorize an Odoo write.
- Load confirmation and execution must still materialize and validate the full
  immutable execution snapshot, submitted snapshot hash, current workspace,
  target, credential bindings, and write identity.
- Diagnostics and job status responses must not contain source values, Odoo
  business values or numeric identifiers, formulas, credentials, tokens,
  request bodies, or raw URLs.
- A previous current report remains authoritative until a new report is
  published atomically.

## Proposed data-manager experience

### Normal work

When a data manager opens Overview or submits an action, retain the page's
local progress treatment. For an ordinary navigation, show a small contextual
label such as **Opening project overview...** only when it lasts longer than
two seconds. Do not show a global warning immediately.

### Work taking longer than expected

When Impodo has an instrumented foreground operation and health checks are
temporarily delayed, show an amber, non-blocking state after a qualified grace
period:

> **Impodo is still working**
>
> This is taking longer than usual. Keep this tab open and do not repeat the
> action.

Do not include restart advice in this state. Preserve unsaved entries, retain
the operation identity, and keep checking both the operation outcome and
server health.

### Connection interrupted

Use the red state only after the longer disconnected threshold is met and no
authenticated Impodo response of any status has arrived:

> **Connection to Impodo was interrupted**
>
> Impodo has not answered recent connection checks. Keep this tab open while it
> tries to reconnect. Do not repeat the last action yet.

Only this state should expose restart and diagnostic-bundle guidance. A later
authenticated application response proves that the connection is restored.
After a successful response, change the message to **Impodo is responding
again** and direct the data manager to check the saved outcome before retrying.
An error response clears the disconnected classification but keeps the owning
operation's error visible.

### Session ended

Keep HTTP 401 as the separate **This Impodo session has ended** state. A 401 is
not a performance problem and must not be folded into busy or disconnected.

## Browser operation and connectivity model

Do not represent these conditions as one flat enum. `BUSY` and connected can
both be true, and a successful heartbeat must not erase the active-operation
state. Track two small state machines and derive the presentation from them.

| Connectivity state | Evidence | Recovery |
| --- | --- | --- |
| `CONNECTED` | An authenticated application response of any status has arrived inside the disconnected window | Keep health and relevant status checks running |
| `DISCONNECTED` | No authenticated application response of any status arrives across the disconnected window, and recent checks end in a timeout or network error | Reconnect, then check the operation outcome |
| `SESSION_ENDED` | An authenticated endpoint returns 401 | Use the newest Impodo session; preserve entries in the old tab |

| Operation state | Evidence | Presentation |
| --- | --- | --- |
| `IDLE` | No instrumented foreground action or known job is active | No operation message |
| `BUSY` | An instrumented same-origin navigation, form action, or known job is active inside the normal grace period | Contextual progress only |
| `DELAYED` | Foreground work is active and successful health or status responses have missed the qualified grace window | Amber **Impodo is still working** |
| `OUTCOME_UNKNOWN` | A mutation request timed out before a receipt or durable read-back established its outcome | Preserve its operation identity and prohibit automatic replay |

Derive the visible state in this order: `SESSION_ENDED`, `DISCONNECTED`,
`OUTCOME_UNKNOWN`, `DELAYED`, `BUSY`, then no global message. An
`OUTCOME_UNKNOWN` operation may also be disconnected; retain both facts so
reconnection leads to receipt read-back rather than replay.

The first qualification values should be:

- heartbeat interval: four seconds;
- heartbeat request timeout: four seconds rather than two;
- one or two missed checks during known foreground work: remain silent;
- approximately 20 seconds without a response: enter `DELAYED`;
- at least 45 seconds without an authenticated Impodo response of any status,
  with a recent timed-out or failed follow-up check: enter `DISCONNECTED`.

These are starting values for tests, not permanent performance targets. Tune
them from representative Windows and macOS evidence. Improving the underlying
work has priority over lengthening the thresholds.

Maintain separate monotonic timestamps for the last authenticated application
response and the last successful response. The first determines connectivity.
The second determines whether known work is progressing normally. Do not base
the state machine only on consecutive failure counts because a delayed timer,
computer sleep, or background-tab throttling can otherwise manufacture a
false disconnection. After a tab becomes visible or the computer resumes,
perform a new bounded check before showing the red state.

The implementation must classify responses correctly:

- HTTP 200 with the expected health body proves health.
- A successful job or operation-status response also proves responsiveness.
- HTTP 401 proves that the server responded and the session ended.
- HTTP 429, 500, or 503 updates the last-response time because it proves that a
  server answered. It does not update the last-success time. It may indicate a
  busy or failed application request, but it is not a disconnected process.
- An abort caused by the heartbeat timeout is only a missed check.
- A browser network error is stronger evidence, but still requires a bounded
  follow-up check before restart guidance appears.

Do not attempt to read the browser tab's loading wheel. Browsers do not expose
it as a reliable application liveness contract. Instead, instrument Impodo's
own same-origin links, forms, asynchronous operations, and job pollers. Store
only a bounded operation type and start time in the active page; never put form
contents or source values in browser storage.

## Performance design

### 1. Stop full evidence loading in shared navigation

Introduce an application query service such as
`WorkspaceNavigationQueryService`. It returns an immutable
`WorkspaceNavigationFacts` read model containing only the facts needed to
choose stage and page status. The web presenter maps those facts and the
current route to labels and links; it does not call stage services or evidence
repositories itself.

`WorkspaceNavigationFacts` is display-only and is not durable workflow
evidence. Each fact must carry or be checked against the current identifier or
content hash owned by its stage. A missing, malformed, or stale binding makes
that stage current, locked, or in need of attention. It must never make a
stage appear complete.

For the load stage, reuse the `ExecutionPreviewSummary` proposed in
[the large-comparison responsiveness plan](responsive-final-review-comparison.md#4-store-a-compact-load-review-projection).
Build that summary once from the exact execution snapshot during comparison.
Store it under the preflight run identifier in the same DuckDB transaction as
the readiness report and current pointer. Do not introduce a second
navigation-owned execution summary.

Navigation then reads only bounded facts such as the current preflight run
identifier, comparison status, total write count, blocker presence, current
execution status, and reconciliation status. It does not read or parse the
execution snapshot, comparison manifest, row decisions, field intents,
relationship links, target-record snapshot, or large report JSON.

The DuckDB adapter should select explicit scalar or compact-summary columns in
one read transaction. It must not call `ExecutionService.current_preview` from
shared navigation. It must also avoid `SELECT *` from evidence tables that
contain large JSON values.

### 2. Keep synchronous page construction off the event loop

Treat common rendering, owner-view reads, navigation construction, DuckDB
access, artifact reads, JSON parsing, and Jinja rendering as synchronous work.
Run the complete page builder in the bounded thread pool instead of moving
individual nested calls.

This is event-loop containment, not the complete performance fix. A CPU-heavy
JSON parse in a Python thread can still compete for the interpreter and memory;
the compact read model removes that work from ordinary pages altogether.

If a remaining row-scale computation still prevents health and status requests
from meeting their budget, move the credential-free CPU phase into a bounded
spawned worker process. Never serialize credentials or writer capabilities
into that worker command.

### 3. Batch canonical owner and stage reads

Replace the chain of independent owner-view lookups with one foundation
repository method that reads the verified Project, MigrationWorkspace,
DataVersion, MigrationRun, and target setup in one registry read transaction.
The source package belongs to a separate DataVersion store and is not needed
by common owner or navigation presentation.

The foundation registry and each workspace engine are separate stores. Do not
simulate a distributed transaction or join them through presentation code.
Resolve access and canonical lineage from the registry first. Pass the
verified owner identifiers into one workspace-engine read transaction that
returns the navigation facts. Run the schema check once for each opened
database connection, not once for every small query. Schema upgrades remain
explicit, fail closed, and transactionally safe.

In-memory job registries may add their bounded active-job status after the
durable read. They must not cause artifact materialization or override a stale
durable binding.

### 4. Add a cross-request cache only if measurements justify it

Do not add a shared navigation cache in the first repair. A single bounded
registry read and a single bounded workspace-engine read should be measured
before adding cache lifecycle and invalidation responsibilities.

If later evidence justifies a cross-request cache, key an immutable result by
the complete set of current stage-evidence identifiers and content hashes,
plus the relevant job-state generation. The workspace revision alone is not a
sufficient key because stage repositories and background jobs can change
without one common revision update. Treat invalidation as an optimization:
binding checks must still prevent an old cache entry from showing stale work
as complete.

Do not cache credentials, complete forms, source rows, Odoo records, large
artifact JSON, or mutable operation outcomes.

### 5. Keep navigation dependencies directional

Use this dependency direction:

```text
route -> navigation query service -> query ports -> DuckDB adapters
  |                                      |
  +-> presenter <- WorkspaceNavigationFacts
```

The application query service owns access, current-binding, and conservative
fallback rules. DuckDB adapters own bounded SQL and storage decoding. The
presenter owns labels, links, and the viewed-page marker. The browser owns
only operation and liveness presentation; it must not reconstruct workflow
completion from URLs or local storage.

Avoid shortcuts that create hidden coupling. Do not monkey-patch
`current_preview`, inspect only the beginning of a large artifact, memoize
mutable workflow state in a module global, scatter navigation-cache
invalidation calls across routers, or catch evidence failures and mark a stage
complete.

### 6. Keep Overview deliberately small

Overview should render the project identity, current stage, last completed
stage, and next safe action from compact evidence. Detailed stage diagnostics,
large record counts, row-level issues, and historical artifacts should load
only on their owning pages and should be paginated where applicable.

## Restore trustworthy performance evidence

Repair the diagnostic-recorder lifecycle so every production child either:

- records `application_started`, request completions, event-loop delays, and a
  terminal lifecycle event; or
- records one bounded diagnostic-unavailable reason that identifies a logging
  ownership or lock failure without exposing paths or data.

Add allowlisted timings for:

- access-context resolution;
- canonical owner-view read;
- navigation-facts read;
- navigation presentation;
- thread-pool queue wait;
- workspace database connect and schema check;
- artifact materialization and parsing when an owning action legitimately
  needs it;
- template rendering; and
- total request time.

The diagnostic bundle should summarize the recent slow route templates, their
safe phases, event-loop delay, DuckDB connection and schema-check counts, and
job phases. It must retain the current privacy exclusions.

## Delivery slices

### Slice 0 — reproduce and restore measurements

- Add a deterministic mature Product fixture with at least 25,000 prepared
  records and a large immutable execution snapshot.
- Reproduce Overview, Match data, Final review, and Check changes in a fresh
  process while probing authenticated health concurrently.
- Repair the missing application and request diagnostic records.
- Preserve raw local timing results for comparison without committing business
  data or machine-specific paths.

### Slice 1 — remove the shared-navigation hot path

- Stop calling the full execution preview from navigation.
- Add `WorkspaceNavigationQueryService`, its query ports, and the immutable
  `WorkspaceNavigationFacts` read model.
- Reuse the compact `ExecutionPreviewSummary`; do not add a second
  navigation-owned execution projection.
- Batch canonical owner reads in one registry transaction and navigation facts
  in one workspace-engine transaction.
- Run complete synchronous page construction outside the event loop.

This is the highest-value performance slice because it removes record-scale
work from every mature workspace page, including Overview.

### Slice 2 — ship operation-aware liveness

- Implement the operation and connectivity state machines, their presentation
  priority, and response classification.
- Instrument same-origin navigations, forms, mutation receipts, and job
  pollers.
- Record any authenticated application response as connectivity evidence, and
  record successful responses separately as progress evidence.
- Replace the current red copy with the amber delayed and red disconnected
  messages.
- Keep automatic mutation retries prohibited.

### Slice 3 — decide whether a navigation cache is warranted

- Measure the bounded uncached query under the mature fixture.
- Add no shared cache when it already meets the request budget.
- If a cache is justified, key it by the complete current-binding set and job
  generation rather than workspace revision alone.
- Add stale-binding, job-transition, and process-restart tests before enabling
  the cache.

### Slice 4 — qualify remaining row-scale work

- Measure full comparison, load confirmation, execution, and reconciliation
  separately from navigation.
- Move remaining CPU-heavy credential-free work to a bounded process only when
  the measurements prove it is needed.
- Set supported Windows and macOS budgets from repeated clean-process runs.

## Acceptance checks

- Opening Overview in the 25,000-record fixture does not materialize the
  execution snapshot or comparison manifest.
- Overview and shared navigation do not decode large report, target-snapshot,
  or row-decision JSON columns.
- Overview and shared navigation perform a bounded amount of work independent
  of prepared-record count and snapshot size.
- One Overview request opens at most one checked foundation-registry connection
  and one checked workspace-engine connection for owner and navigation facts.
- A warm Overview meets a proposed local p95 of 1.5 seconds and a cold Overview
  meets a proposed local p95 of 3 seconds on each qualified development
  platform.
- Authenticated health and active-job status meet a proposed p95 of 250 ms and
  remain below one second while an ordinary workflow page is being built.
- A 60-second deliberately slow but progressing operation shows the amber
  delayed message and never shows disconnected while authenticated progress
  responses arrive.
- A stopped test server reaches the red disconnected state within the bounded
  recovery window.
- Repeated HTTP 500 and 503 responses do not claim that the server is
  disconnected while those responses continue to arrive.
- HTTP 401 continues to show the separate session-ended state.
- A timed-out mutation is never repeated automatically and keeps its operation
  identity until receipt read-back resolves the outcome.
- Every completed request produces one privacy-safe diagnostic record, and a
  delayed event loop produces bounded delay evidence.
- The diagnostic bundle continues to exclude source and Odoo values, formulas,
  credentials, tokens, request bodies, headers, and raw URLs.
- Load authorization still validates the complete immutable execution snapshot
  and all current bindings before any Odoo write begins.

## Verification scope

- JavaScript tests with fake time for every liveness transition, response
  class, recovery, navigation marker, and operation receipt.
- Integration tests that hold a synchronous page dependency and prove that
  `/health` and job status continue to respond.
- A route test proving that Overview and common navigation never call
  `current_preview`, decode a large evidence column, or materialize an
  execution artifact.
- DuckDB integration tests for the batched owner view, one-read navigation
  facts, schema upgrade, conservative missing-evidence behavior, and
  stale-binding rejection.
- Fault-injection tests for a health timeout, network refusal, HTTP 500, HTTP
  503, 401, child restart, and diagnostic-lock failure.
- Fresh-process performance tests for cold and warm Overview, all main workflow
  pages, concurrent health latency, CPU time, working-set growth, DuckDB
  connections, schema checks, and artifact bytes read.

## Expected code ownership

| Responsibility | Proposed or affected code |
| --- | --- |
| Browser operation and connectivity state machines and copy | `src/impodo/web/static/server-recovery.js`, `src/impodo/web/templates/base.html` |
| Same-origin navigation and form activity | a small shared browser controller under `src/impodo/web/static/` |
| Common render containment | `src/impodo/web/presenters/common.py` and workspace routes |
| Navigation query contract and conservative status rules | a new application read model and query service under `src/impodo/application/workspace/` |
| Compact workflow navigation presentation | `src/impodo/web/presenters/navigation.py` |
| Canonical owner-view batching | `src/impodo/application/workspace/views.py` and the migration-foundation repository |
| Bounded navigation-fact reads | a dedicated DuckDB query adapter over current pointers and compact stage summaries |
| Compact execution summary publication | the preflight workspace-engine schema and repository described by the large-comparison responsiveness plan |
| Request and loop diagnostics | `src/impodo/web/diagnostics.py`, launcher, and server supervisor |
| Scale qualification | focused integration and performance tests under `tests/` |

## Out of scope

This proposal does not relax comparison or load validation, hide a genuine
process loss, automatically restart a slow process, automatically retry a
mutation, put business data into browser storage, or replace detailed owning
pages with the compact navigation read model.

## Related documentation

- [Keep Final review responsive during large Odoo comparisons](responsive-final-review-comparison.md)
- [Make Match data saves observable and formula-safe](resilient-match-data-saving.md)
- [Developer workflow: Match data](../developer/workflow/03-match-data.md)
- [Workflow evidence lifecycle contract](../developer/contracts/evidence-lifecycle.md)
