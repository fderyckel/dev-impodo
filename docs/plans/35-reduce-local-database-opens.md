---
audience: developer
document_type: plan
status: partially-implemented
---

# Reduce repeated local database work

## Goal and scope

Make pages, saved decisions and Odoo loads respond sooner by removing repeated
local database setup and duplicate queries. Keep the same authorization,
prepared evidence, proposed writes, durable receipts and recovery results.

This is an implementation plan, not a claim that performance has improved.
The private diagnostic performance history informed the priorities. Customer
measurements, attempt identifiers and diagnostic files remain outside Git.
Reproduce the relevant shapes with synthetic data before assigning numerical
budgets to the current source.

Implement this work in the order below. Deliver a measured improvement from
each package before broadening the refactor. The
[responsiveness plan](09-responsive-workflow-liveness-and-navigation.md) owns
the wider platform latency qualification. This plan owns the reduction in
physical opens, schema work and SQL on the selected paths. It does not change
the cross-plan order in the [roadmap](00-remaining-work.md).

## Implementation progress

The first implementation covers Prepare data review and its decision
commands, saved load landing, review, confirmation and outcome pages, and
initial load progress HTML. These paths use bounded local owners and keep
blocking reads off the event loop. JSON load polling remains in memory.
Focused fixtures cover both Odoo 19 and 20 through local and remote access,
stale decisions, denied access, rendering failure and independent validation
on the next request. Windows latency and the restarted live session remain
unverified.

The second implementation consolidates Match data commands and load
submission. Local commands retain their receipt and rejection reads within one
operation. Remote checks and default refreshes close owners before Odoo work
and recheck captured inputs in a new publication operation. Submission keeps
both intent confirmations and releases owners before dispatch. Controlled
fixtures cover both majors and access modes, including stale requests and
owner release. The Stage 3 follow-up below removes repeated reads from that
publication phase and retains the comparison with the captured inputs.

The third implementation bounds worker admission, journal lifecycle and
receipt phases through the existing local-operation port. It retains the
repository mutability checks, independent transactions and committed read-back.
A real-store mixed-write fixture measures the complete queued worker for both
majors and access modes, including verification and correction publication.
Separate assertions cover the write stage, journal phases and progress.
Restart and unknown-outcome cases retain receipts and prevent duplicate writes.

Verification and correction publication remain planned below. Journal phases
now have bounded owners; further SQL consolidation still requires evidence of
redundant reads. The table in section 2 records the baseline before these
implementations; the page and command scope changes have now been implemented.
Windows latency and the restarted live session remain unverified.

## Design to retain

Use the existing
[local operation helpers](../../src/impodo/web/composition/page_reads.py).
One complete synchronous local phase runs on one worker and reuses the owners
it needs. Each repository transaction still commits or rolls back
independently. A retained database owner is the physical open instance;
repository cursors are separate and must not be counted as physical opens.

Close every owner before an await, remote Odoo call, background dispatch or
streaming response. Each later phase and request revalidates its stores.
Never wrap the complete load, comparison or reconciliation in one retained
scope: those operations perform remote I/O between local phases.

Application services should accept the existing
[`LocalOperation` port](../../src/impodo/application/shared/local_operations.py)
where they need to describe these phases. Composition supplies
`run_local_operation_sync`; application code must not import web helpers.
Preserve the supported storage routes and their authority rules, including
the shared-server boundary.

Keep connection configuration stable where semantics permit. Preserve the
security settings, memory limits and thread limits. Do not introduce a global
connection pool, cross-request owners, new concurrency defaults or a second
implementation of the workflow.

## 1. Establish the current baseline and owner ledger

Use one fixed source snapshot, Python 3.12 and the pinned dependencies. Build
synthetic fixtures for an action-rich review page, saved mapping commands,
load submission, and a complete load with fixed Odoo responses. Include both
creates and updates, dependent relationships, rejected rows and recovery.

For each complete GET, POST, rejected response and worker phase, record:

- Count physical opens by store and effective configuration in the isolated
  fixture. Identify repeated access to the same owner and required
  configuration changes.
- Count SQL statements and catalogue queries with
  [`count_duckdb_statements`](../../tests/support/database_probe.py). Preserve
  its cursor registration so the probe does not disable schema reuse.
- Record schema checks across registry, Data version, workspace linkage and
  workspace engine stores. Include `ensure_current_schema` and the execution
  repository's structural validation.
- Record elapsed time, connection setup, schema time, lock retries and wait,
  CPU time, sampled memory, and the available file and logging costs.
- Separate submission from the worker. Within the worker, distinguish local
  admission, source reads, before-call journals, remote transport, after-call
  journals, progress, verification and completion publication.

Reuse the existing diagnostics. Add only missing bounded phase attribution
needed to distinguish SQL, commit and close work in a reproduction. Keep SQL,
parameters, paths and source values out of routine exports. Measure any added
diagnostic overhead separately.

The private capture reported additional opens inside journal phases. Current
`ExecutionRepository.record_batch_started` and `_record_outcomes` each have
one direct connection block. Attribute the additional opens through the
complete composed call before choosing a repair. The captured build and the
current checkout are not interchangeable baselines.

**Exit:** Each selected path has an owner ledger and a regression case that
exposes redundant opens or SQL in the unchanged candidate. Retain customer
observations privately; keep only synthetic evidence in repository tests.

## 2. Consolidate page admission and local commands

Start here because these changes address the repeated wait on ordinary
navigation and establish the same ownership pattern for later work.

| Path and current owner | Planned change |
| --- | --- |
| Prepare data review in [`normalization.py`](../../src/impodo/web/routers/normalization.py) uses `run_page_read` after separate middleware admission. | Move admission, journey checks, child reads, navigation and rendering into `run_workspace_page_read`. |
| Local load landing, review, confirmation and outcome pages in [`execution.py`](../../src/impodo/web/routers/execution.py) have separate admission and page scopes. | Consolidate the eligible GETs through the same admitted-page mechanism. Audit progress and polling separately so a light status request does not gain full preview reads. |
| Prepare data decisions use plain `run_in_threadpool`, direct error rendering and a synchronous state read after approval. | Use one bounded local command for the mutation and required local response facts. Render rejected forms in a bounded read after the command has rolled back and released its owners. Keep all local database work off the event loop. |
| Match data commands in [`mapping.py`](../../src/impodo/web/routers/mapping.py) split command admission, dataset preparation, saving and receipt reads across scopes. | Consolidate each action's contiguous local work. Preserve receipt read-back and uncertain-save recovery. Close the scope before matching-access probes and remote supporting-value capture; recheck bindings in a new publication scope. |
| Load submission in `execution.py` separates context, authorization, preview, intent and enqueue preparation. | Assemble one authorized local submission result for each contiguous local phase. Preserve the fresh worker-entry checks. Dispatch the job only after all owners have closed. |

Extend the explicit admitted-route handling in
[`WorkspaceAccessMiddleware`](../../src/impodo/web/security.py) together with
each GET. Authorization and closed-workspace or Recipe journey policy must
precede child-store reads and active-job inspection. Do not retain an owner
across middleware `call_next`.

Keep the existing trusted job context and in-memory JSON status path cheap.
The initial progress HTML has separate local foundation reads; move those
reads off the event loop in their own bounded page callback. Do not introduce
full preview or registry reads on every polling request.

The existing saved-page fixture proves four physical opens and three schema
checks for its required stores and configurations. Use that ledger as a model,
not a universal one-open target. Derive the new normalization and load-page
ceilings from their actual data needs. Preserve the existing consolidated
Match data and Final review GET budgets.

**Exit:** Complete requests have lower fixture-derived open and SQL ceilings,
produce the same saved decisions and responses, and release all owners on
success, denial, redirect and rendering failure. The next request opens and
validates independently. Job dispatch and remote probes observe no retained
owner.

### Stage 3 Check matches

On the ordinary **Check matches** path, a valid result now opens **Confirm
matches** directly. An invalid result opens the first correction in **Check
and resolve**. If exclusions need confirmation or a row rule needs correction,
the check opens **Checked source rows**. The operator still decides whether to confirm
the selection and submit the mapping. The
[Stage 3 guide](../user/workflow/03-match-data.md#steps-in-impodo) describes
those decisions. The optional **Check identities and update suggestion**
command belongs to the separate table-ordering workflow.

After the read-only Odoo work, the
[mapping service](../../src/impodo/application/workspace/mapping/service.py)
reads the current selection, schema and matching rules once. It verifies
them against the inputs used before the Odoo call, then validates the
mapping with those same fresh reads. A receipt lookup or replay returns the
decision for the original check only while its result is current. The
[receipt tests](../../tests/integration/web/test_mapping_check_receipts.py)
cover a changed credential binding and a new check on the same revision.

The [command tests](../../tests/integration/web/test_mapping_command_performance.py)
bound SQL, physical opens and schema checks for Odoo 19 and 20 in local and
remote mode. The [result-page tests](../../tests/integration/web/test_mapping_journey_mutations.py)
exercise the initial GET and a warm reopen. Both read only locally saved
evidence. The [synthetic timing runner](../../tests/performance/check_matches_runner.py)
separates the check POST, result GET and reopen for a valid mapping, a missing
field and two tables with repeated linked values. An instrumented run counts
database work separately from the elapsed-time samples.

On arm64 macOS with Python 3.12.14 and DuckDB 1.5.5, three pairs of fresh
processes per fixture ran the baseline and candidate in alternating order.
Each used the Odoo 20 remote adapter with fixed fictional responses. The
elapsed times below are medians for the check POST plus its first result
GET. The repeat is a new check of the same mapping in the same workspace.
SQL counts cover the POST only.

| Fixture | First check SQL, before → after | Repeat SQL, before → after | First check and GET, ms, before → after | Repeat check and GET, ms, before → after |
| --- | ---: | ---: | ---: | ---: |
| Valid mapping | 282 → 264 | 162 → 144 | 750 → 951 | 523 → 688 |
| Missing required field | 271 → 253 | 151 → 133 | 515 → 475 | 371 → 437 |
| Two tables, 1,000 rows sharing two linked values | 446 → 428 | 326 → 308 | 1,372 → 1,216 | 926 → 1,244 |

The check saved 18 statements in every case. Physical opens and schema
checks stayed level. The linked fixture made one record query for both
distinct values. Every result GET and reopen used four opens and three
schema checks and made zero Odoo calls. Wall times varied by fixture and
did not show a repeatable overall improvement. Repeat checks for the
missing-field and linked fixtures took longer in all three pairs. Local
admission was the largest measured POST phase for valid and missing-field
checks. Reading and saving supporting values took the most time for the
linked check.

The next measurement isolated the linked check on the same 1,000-row
fixture. Each of the two physical tables held the same two category names.
Sharing the frozen source metadata within the lookup reduced source-package
reads from six to four. Applying row rules before deduplicating the input
columns reduced the tuples passed to reference preparation from 1,000 to
four. The check still sent both category names in one Odoo record query.

| Linked check | Before | After |
| --- | ---: | ---: |
| SQL statements to collect source keys | 85 | 57 |
| SQL statements to capture supporting values, including collection | 123 | 95 |
| SQL statements for the first check POST | 428 | 400 |
| SQL statements for the repeat check POST | 308 | 280 |
| Median first check and result GET, ms | 569 | 730 |
| Median repeat check and result GET, ms | 455 | 697 |

These timings compare three alternating pairs of fresh processes on arm64
macOS with Python 3.12.14, DuckDB 1.5.5 and the fictional Odoo 20 remote
reader. The candidate took longer in two of the three pairs on both the
first and repeat check. This change reduced local SQL and repeated input
processing. The measured server response times did not show a consistent
speedup. The repeat POST still opened 20 database connections and ran 15
schema checks.

#### Time spent saving a linked check

Four new alternating pairs on the same two-table fixture compared the code
before and after the source-key change (builds `849b02a2` and `3237da45`).
The median time for the repeat POST and its result GET went from 418 to
421 ms. The earlier 455 to 697 ms increase did not recur. The linked-value
phase shortened in all four pairs.

A separate instrumented repeat on build `3237da45` measured 342 ms for
the POST. Admission took 125 ms, collecting source keys took 64 ms and the
fictional Odoo callbacks took less than 1 ms. Saving the linked choices
took 55 ms, followed by 90 ms to validate and save the mapping and respond.
Each of those last two steps spent about 32 ms opening four databases and
12 ms on three schema checks. Database timings are included in the phase
times. The supporting save also had about 8 ms of scope overhead.

Impodo now saves the verified choices and checks the mapping in one local
operation after Odoo returns. The two saves retain their own transactions
and freshness checks. Four further alternating pairs compared that change
(`78768bcf`) directly against `3237da45`. These server timings use fresh
processes, arm64 macOS, Python 3.12.14, DuckDB 1.5.5 and fictional Odoo 20
remote responses. Timing runs had the SQL and database probes disabled.

| Linked fixture | Separate local operations | Shared local operation |
| --- | ---: | ---: |
| Median first POST and result GET, ms | 503 | 460 |
| Median repeat POST and result GET, ms | 463 | 373 |
| Median repeat POST alone, ms | 355 | 275 |
| Median repeat result GET alone, ms | 108 | 98 |
| Repeat POST physical opens | 20 | 16 |
| Repeat POST schema checks | 15 | 12 |
| Repeat POST SQL statements | 280 | 269 |

Every pair finished sooner on both the first and repeat linked checks.
Each check issued one Odoo record query. The result GET and reopen each
used 128 SQL statements, four opens, three schema checks and zero Odoo
calls in both builds. The simple mapping had no linked choices to save.
Its repeat POST held at 144 statements, 12 opens and nine schema checks,
and its repeat timings improved in two of the four pairs.

#### Checking source readiness

The mapping form and its result page both verify that the source is ready.
The file-source check now reads the source mode and frozen selection identity
without loading the file list or later-stage results. An Odoo source also
requires a current schema and matching capture plans. A Recipe workspace
can supply its frozen selection through its verified Data version.

On the linked fixture, the file-source check fell from 18 SQL statements to
9. The repeat POST fell from 269 to 260 statements and its result GET from
128 to 119. The POST still used 16 physical opens and 12 schema checks.
The result GET used four opens and three schema checks. The workspace overview
continued to read full navigation in 29 statements.

Six alternating process pairs per fixture compared builds `81f767f9` and
`b78cc474` on arm64 macOS with the fictional Odoo 20 remote reader. On a
repeat check, the median source check went from 61.8 to 47.6 ms for linked
values and from 75.3 to 67.3 ms for the simple mapping. The linked repeat
POST and result GET together went from 782 to 725 ms, but only two of the
six pairs finished sooner. The simple mapping's first POST and result GET
went from 840 to 891 ms. The timings establish no consistent improvement
for the complete request.

#### Reusing the frozen source package during Check matches

While reading the mapping selection, Impodo verifies the complete frozen
Data version package, including the datasets selected for a Recipe. A new
check uses that package again when it reads the current workspace settings
for Odoo access. The command verifies the package's project and Data version
before reuse. After Odoo responds, publication reads the current inputs again.

On both file-source fixtures, this removed one package reconstruction and
eight SQL statements per check. The linked first and repeat POSTs went from
380 to 372 statements and from 260 to 252. For the simple mapping, they
went from 255 to 247 and from 135 to 127. Physical opens and schema checks
held steady. Opening or reopening the result still used 119 statements.

Paired macOS runs compared builds `0a0402b4` and `c193d022` with a
fictional Odoo 20 remote reader. On the two-table, 1,000-row fixture, the
median time to prepare Odoo access for a repeat check fell from 8.0 to
5.3 ms. All four pairs spent less time in that step, while only two finished
the complete POST and result GET sooner. With 128 source columns and eight
rows per table, the median repeat access step went from 28.1 to 5.7 ms.
Both wide-table pairs also finished the complete request sooner. The wide
fixture had only two pairs.

#### Verifying the package hash

The Data version reader computes the package hash once per read and checks
it against both stored hashes. On the linked fixture, the reader's full-package
hash calculations fell from 28 to 14 on the first POST, from 20 to 10 on the
repeat POST, and from eight to four when opening the result. Each request
issued the same number of SQL statements as before.

Six alternating macOS process pairs compared builds `c193d022` and
`1c6b1ab7` against identical DuckDB files. Each pair timed nine reads per
build after one warmup. Median time per verified read fell from 1.38 to
1.25 ms for two three-column tables and from 11.60 to 9.09 ms for two
128-column tables. Both fixtures improved in all six pairs. Two separate
HTTP pairs per fixture had mixed repeat POST and result GET timings.

#### Validating categorical values across tables

After Odoo supplies linked values, validation reads the frozen selection
and snapshot list once for the physical tables it checks. The two-table
fixture previously read each three times. Reuse reduced this part of
validation from 39 to 13 SQL statements and from three verified source
package reads to one. Both tables still required verification of their
individual Parquet files. A new check reads the metadata again.

The first **Check matches** POST went from 372 to 346 statements and the
repeat POST from 252 to 226. Their physical opens and schema checks held
steady. Opening and reopening the result each used 119 statements.
The simple mapping had no categorical values to check and its SQL counts
held steady.

Four alternating macOS process pairs compared builds `e5c70b84` and
`48e4a98d` on the two-table, 1,000-row linked fixture. Median repeat
categorical validation fell from 13.99 to 6.47 ms. All four first checks
reached the result page sooner, and two of the four repeat checks did so.
Two additional pairs used eight rows and 128 source columns per table.
Median repeat categorical validation on that fixture fell from 42.24 to
18.71 ms. These timings use an in-process HTTP client and a fictional
Odoo 20 remote reader.

**Remaining qualification.** Measure from the click until the correct control
is visible and keyboard-usable on matched macOS and Windows fixtures. Record
first and repeat checks separately with at least three comparable runs per
candidate on each host. The in-process timing runner measures server response
time. Browser feedback, network transit and painting need separate evidence.

## 3. Bound local work around each Odoo write

Trace [`ExecutionService`](../../src/impodo/application/workspace/execution/service.py),
[`LoadWriter`](../../src/impodo/application/workspace/execution/writer.py),
[`JournalTransitions`](../../src/impodo/application/workspace/execution/journal.py),
load-control reads and
[`ExecutionRepository`](../../src/impodo/adapters/duckdb/execution_repository.py)
as one composed path. Identify repeated authorization, snapshot, journal and
load-control reads before changing their ownership.

Start with confirmed lifecycle duplication: `start_run`, `record_recovery`,
`close_interrupted_run_for_recomparison` and `finish_run` check mutability
through a separate open before their mutation connection. Share the owner
within a bounded local operation while preserving the fresh checks and
transaction boundaries. The successful before-call and after-call journal
transactions already read their required controls and row identities. Those
checks are not redundant merely because both sides of transport need them.

Introduce local-operation boundaries only around contiguous local work:

1. Capture and validate the execution inputs in one bounded admission phase.
   Release the owners before target identity or relationship lookups.
2. Recheck the state required for the next permitted write and persist its
   exact in-flight batch before transport. End the local scope before Odoo.
3. Persist the definitive result immediately after the call, including private
   rejection detail where applicable. Update operation-owned progress from
   committed transitions, then release the local scope.
4. Keep later load-control decisions, rejection splitting, cooldown, pause,
   cancellation and uncertain outcomes at their existing semantic boundaries.

Reuse immutable operation inputs only when their existing bindings permit
it. Keep mutable authorization, lifecycle and load-control checks current.
Remove duplicate reads inside a phase before adding any new cached state.
Keep snapshots and journal indexes bounded; do not trade connection work for
an unbounded in-memory copy of all evidence.

After physical opens are bounded, profile the journal SQL. Consolidate proven
redundant reads or use bounded bulk operations where the transaction already
owns the exact batch. Preserve deterministic ordering, per-row evidence and
atomic private-detail publication. A successful remote call must retain its
receipt even when subsequent progress or detail handling fails.

Review the repository's schema-validation markers explicitly. Successful
validation may be reused only for the same retained owner, configuration and
exact contract. Do not extend repository-lifetime path markers to new
responsibilities. Keep lineage checks on every access, discard failed checks,
and validate again after reopening.

**Exit:** A real-store worker fixture bounds the complete load, not just its
submission or an in-memory journal double. Each fake remote reader and writer
asserts that no local owner remains open. Before-call and after-call evidence
remains durable through interruption and recovery.

## 4. Consolidate verification and completion publication

Use the same local-operation port in
[`ReconciliationService`](../../src/impodo/application/workspace/execution/reconciliation.py).
Capture the authorized snapshot, journal and metadata locally, close owners
for read-back, then publish only after rechecking the relevant bindings.
Keep remote ancestor and relationship reads outside every retained scope.
Measure local work separately from the complete read-back interval.
Group the separate mutability read and mutation owner in the result repository
within its publication phase. Preserve compensation if protected evidence is
saved but publication of the compact result fails.

Audit repeated `current_preview`, migration-run and completion reads in
[`preflight.py`](../../src/impodo/web/routers/preflight.py), including Recipe
completion and no-change results. The comparison's existing `bind_start`
phase is already bounded; retain it and repair the remaining local tails.

Bound the pure-local capture and publication work in
[`CorrectionWorkflowService.publish_completed_load`](../../src/impodo/application/correction_workflow.py).
Check inexpensive eligibility and existing state before acquiring evidence
that the existing contract does not need for an ineligible case. Preserve
idempotency, exact completed-load bindings and atomic publication. A missing
required prepared snapshot must retain its specific warning and recovery
meaning; suppressing it is not a performance fix. The
[post-load continuation plan](29-post-load-continuation-and-recipe-saving.md)
continues to own changes to that product behavior.

**Exit:** Verification and completion each have separate local and remote
measurements, structural budgets and unchanged business results. A completion
warning cannot turn a verified Odoo write into a failed load or trigger
another write.

## Budgets and regression coverage

Derive the physical-open budget from the required owner acquisition lifetimes
in each local phase. Count a necessary configuration transition explicitly;
switching the same file from configuration A to B and back can reopen an
owner. Remove avoidable alternation rather than weakening configuration to
make a counter pass.

For a load with `B` actual remote write attempts, including split attempts,
derive the budget as fixed local admission and completion work, plus each
attempt's required before-call and after-call owners, plus separately counted
lookup and recovery phases. Durable writes on both sides remain required.
One workspace-engine owner on each side is a valid target only when the
fixture proves that those phases need no additional store or configuration.
Never promise one connection for the whole load.

For each fixture, pair the open ceiling with schema and SQL ceilings that
fail on the redundant implementation. Count catalogue queries separately and
explain which exact contracts cause them. Avoid wall-clock assertions in
portable tests. Assert values, ordering, hashes, authorization, receipts and
recovery alongside performance structure.

Extend these existing suites and add a whole-worker real-store budget suite:

| Responsibility | Existing regression owners |
| --- | --- |
| Worker ownership, cleanup and admitted pages. | [`test_page_reads.py`](../../tests/integration/web/test_page_reads.py), [`test_page_read_connections.py`](../../tests/integration/duckdb/test_page_read_connections.py), [`test_workspace_page_admission.py`](../../tests/integration/web/test_workspace_page_admission.py), and [`test_scoped_store_validation.py`](../../tests/integration/duckdb/test_scoped_store_validation.py). |
| Prepare data reads and decisions. | [`test_normalization_presenter.py`](../../tests/integration/web/test_normalization_presenter.py) and [`test_preparation_workflow.py`](../../tests/integration/web/test_preparation_workflow.py). |
| Match data pages and commands. | [`test_stage235_performance.py`](../../tests/integration/web/test_stage235_performance.py), [`test_mapping_catalog_scale.py`](../../tests/integration/web/test_mapping_catalog_scale.py), [`test_mapping_mutation_recovery.py`](../../tests/integration/web/test_mapping_mutation_recovery.py), [`test_mapping_source_choice_recovery.py`](../../tests/integration/web/test_mapping_source_choice_recovery.py), and [`test_mapping_supporting_recovery.py`](../../tests/integration/web/test_mapping_supporting_recovery.py). |
| Comparison and load submission. | [`test_final_review_performance.py`](../../tests/integration/web/test_final_review_performance.py) and [`test_load_submission_performance.py`](../../tests/integration/web/test_load_submission_performance.py). |
| Journals, remote boundaries, rejection and recovery. | [`test_load_phase_boundaries.py`](../../tests/application/workspace/execution/test_load_phase_boundaries.py), [`test_journal_transitions.py`](../../tests/application/workspace/execution/test_journal_transitions.py), [`test_execution_repository.py`](../../tests/integration/duckdb/test_execution_repository.py), [`test_execution_load_control.py`](../../tests/integration/duckdb/test_execution_load_control.py), and [`test_load_recovery.py`](../../tests/integration/web/test_load_recovery.py). |
| Verification, completion and diagnostic integrity. | [`test_reconciliation.py`](../../tests/application/workspace/execution/test_reconciliation.py), [`test_correction_origin_publisher.py`](../../tests/application/test_correction_origin_publisher.py), [`test_post_load_continuation.py`](../../tests/integration/web/test_post_load_continuation.py), and [`test_load_performance_capture.py`](../../tests/integration/web/test_load_performance_capture.py). |

Cover Odoo 19 and 20 through local and remote access for every affected path.
Include file-source Authoring, supported Odoo-source transfer and affected
Recipe Test and Production journeys. Exercise creates, updates, dependent
relationships, ambiguous lookups, rejected forms, rejected batches, unknown
outcomes, pause, resume, failed publication and no-change completion. Verify
that a new operation rejects stale authorization and changed lineage even
after the previous operation validated successfully.

## Windows verification and delivery

Measure an unchanged and changed fixed candidate on the same real Windows
machine with identical synthetic input, mapping, workflow state, runtime,
library versions and database configuration. Alternate candidates for at least
three repetitions each. Keep cold process starts, repeat runs and interruption
gaps distinct. Use fixed remote responses to isolate local application cost;
label those results as fixture evidence. Never repeat a real customer Odoo
write as a timing trial.

Report elapsed and CPU time, sampled memory, physical opens, setup time,
schema and SQL counts, lock waits, and remaining dominant phases. Keep
inclusive and nested timings separate. Record unavailable storage, power or
security attribution without assigning a cause. Run macOS separately and
claim a Windows gain only after the Windows comparison supports it.

For each implementation package, run its focused tests, the Python quality
gate, documentation checks and complete repository gate. Before delivery,
save the included files and patches, record a cutoff and verify one fixed
candidate. Commit or push only when the maintainer authorizes delivery.
Hosted workflows remain manual and must not be dispatched without an explicit
request. Omitted hosted or Windows evidence remains visible and does not
qualify a public release.

Update current workflow documentation and test registrations only with the
verified implementation. Keep the plan as the sole owner of proposed
behavior until then. Preserve the running Impodo session; a live restart and
same-state confirmation require approval for that interruption. Complete
isolated verification first. Remove this plan when all packages and required
platform evidence have been accepted, and retain the current design and
verification results in their owning documentation.
