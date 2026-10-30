---
audience: developer
kind: plan
status: proposed
---

# Prepare large workspaces containing several kinds of dataset

## Outcome and decision

The data manager should be able to prepare the current 65,299-row delivery,
including its combined table and the additional table, in the same workspace.
They should retain their accepted source data, field matches, explicit blank
decisions, and Recipe rules. They should not need to split the migration into
new projects to satisfy a blanket row limit.

**Proposal, 2026-10-30:** Add one preparation plan that combines bounded
producers for each logical dataset, followed by the existing shared checks.
For a fully bounded route, row count becomes an advisory warning and does not
prevent preparation. Integrity errors and exhausted processing resources
remain blocking conditions. This document proposes work; it does not change
the current limits or authorize implementation.

This plan owns the immediate mixed-workspace delivery. The broader
[preparation efficiency plan](11-preparation-efficiency-and-storage.md) owns
the remaining general evaluator and storage improvements. The broader
[deferred scale track](00-remaining-work.md#1-qualify-related-and-mixed-preparation-at-100000-rows)
does not need to be completed in full before this workspace can work.

## What the current failure establishes

The supplied diagnostic bundle identifies the restarted build containing the
latest matching-reader fix. Preparation failed in `VALIDATING` after about
205 milliseconds, with no completed rows. This is an admission refusal; it
does not establish that processing the data exhausted memory or took too long.
The privacy-safe bundle does not contain the saved source rules or row values.
The exact provider used by the added table still needs verification.

The current preparation service chooses a route for the whole workspace:

- Direct datasets can use the bounded direct session.
- One exclusive reviewed join group can use the bounded grouped session.
- Other combinations fall back to the materialized route.

Adding another mapped table or preparation rule can disqualify the exclusive
join route. The capability manifest then applies the 25,000-row fallback to
transformation, canonical adaptation, quality, normalization, and relationships.
The reported reasons are `DERIVED_OR_NON_DIRECT_DATASET`,
`NON_DIRECT_QUALITY_INPUT`, and `SET_BASED_RELATIONSHIP_ROUTE_UNAVAILABLE`.
These are consequences of the route selection, rather than five independent
measurements of insufficient capacity.

The matching fix qualifies one selected join independently. Complete
preparation still needs to compose those datasets and preserve checks across
them; fixing value enumeration did not implement that orchestration.

## Proposed user experience

The data manager selects **Prepare data** as usual. For a supported bounded
route above an initial advisory threshold of 50,000 physical source rows,
Impodo starts the job and shows a notice such as:

> Preparing 65,299 source rows across several datasets. This may take longer.
> You can follow progress or cancel safely.

This is proposed wording. It requires no additional acceptance checkbox or
second confirmation. Existing matching warnings retain their existing meaning.

The worker reports the current table and phase. It distinguishes source rows
read from logical rows produced: 65,299 input rows do not necessarily produce
65,299 customers. A final summary reconciles each dataset and its lineage.

A tested size of 100,000 rows is an initial qualification target, not a new
automatic rejection threshold. Larger bounded jobs receive an additional
advisory that they exceed the measured range. Configured memory, disk, and
execution budgets control resource exhaustion. Stale evidence, invalid joins,
missing required values, and ambiguous relationships keep their existing
blocking or quarantine policies.

## Implementation proposal

### 1. Compile a route for every dataset and dependency

Replace the workspace-wide direct-or-join decision with a preparation graph.
Each output declares its producer, physical inputs, dependencies, projected
columns, and downstream check capabilities. Qualify each reviewed join using
its selected output, as the matching reader now does.

The first delivery must support direct frozen tables beside reviewed exact
left-join groups, including several independent groups. Verify the actual
additional table's provider before finalizing scope. If it uses an existing
lookup, hierarchy, or parent/child split rule, its bounded producer belongs in
this delivery's acceptance case. A direct-table-only substitute cannot close
the reported workspace failure.

Use native Polars for qualified transformations and bounded Python batches
for existing row-local operations whose native semantics are not qualified.
Cross-row operations use staged facts or set-based reducers; they cannot be
evaluated independently within arbitrary batches. Model names, field names,
and business labels never select a route.

### 2. Feed one durable preparation session

Refactor direct and grouped producers so they append to a session owned by
the workspace preparation operation. Individual producers must not finalize
and publish separate current runs. Evaluate source dependencies in order,
retain stable dataset and row ordering, and aggregate transformation impacts,
control totals, exclusions, findings, and physical lineage in bounded pages.

Record unique physical input rows, logical output rows per producer,
relationship edges, and finding volume separately. Shared inputs must not be
counted as new physical rows every time another output uses them. Multiple
logical records may legitimately reference one physical row.

### 3. Complete shared checks without whole-run reconstruction

After all required datasets have staged their values, build narrow identity,
relationship, issue, and accounting facts. Reuse the existing session indexes
and set-based quality machinery to resolve incoming references across dataset
boundaries. Preserve duplicate detection, missing and ambiguous reference
outcomes, complete identity-group quarantine, and ordinary relationship
propagation as separate contracts.

Target-catalog Many2one references remain portable and offline during
preparation. Preserve saved aliases, ordered company scope, constant existing
references, hybrid resolution, and explicit `blank_source_values`. Preparation
makes no Odoo calls. Normalization and quality consume bounded projections;
large findings and collision groups must not accumulate in Python collections.

An unsupported operator needs a precise table, rule, and stage explanation.
Changing a row-count error into a warning cannot authorize skipping a required
check or loading the entire delivery into the old materialized evaluator.

### 4. Separate row advisories from resource and integrity guards

Change admission to describe whether every required stage has a bounded route.
For those routes, replace `physical_rows <= min(stage_row_limits)` with
admission plus advisory volume information and explicit resource budgets.
Keep the materialized fallback's own safety boundary until that evaluator is
replaced; a large bounded job must never silently fall back to it.

Start with the existing 1,000-row grouped batch size, bounded byte transport,
and 256 MB/two-thread join settings. Measure and adjust them with evidence.
Propose a 1 GiB peak worker-memory target for the motivating fixture, measured
across the complete worker rather than one DuckDB connection. Agree time and
temporary-disk budgets from the baseline before optimization and record changes
to those targets explicitly.

DuckDB ownership remains local to each synchronous phase. Verify and stage
each needed snapshot projection once per producer, then use narrow set-based
joins and indexes. Share compatible intermediate projections only within the
owned preparation session. There must be no connection, schema initialization,
query, or full input scan per source row or matched field, and no process-wide
connection cache. Release owners before asynchronous work or remote calls.

### 5. Publish complete evidence and preserve recovery

Bind the execution plan and all outputs to the current source snapshots,
mapping, schema, source-preparation rules, evaluator versions, and actor.
Stage immutable artifacts and findings before advancing the successful
workspace result. Cancellation, resource exhaustion, and worker failure must
leave the previous complete result usable and must not expose a partial run
as successful. Reuse existing publication transactions and recovery contracts;
do not introduce a transaction spanning files and remote Odoo.

Recipes retain business and preparation rules, including blank decisions.
Each reuse compiles a fresh execution plan against the new delivery and
repeats the checks. Advisory notices and machine-specific resource observations
do not become reusable Recipe identity. Prepare-data qualification does not
increase a later load's independently qualified capacity.

## Delivery and acceptance

| Increment | Required outcome |
| --- | --- |
| A: Reproduce and classify | Inspect the actual saved provider graph through an authorized application read. Capture a privacy-safe capability report. Reproduce the 65,299-row refusal with fictional data before changing admission. |
| B: Compose producers | One session accepts a join group beside another mapped dataset and several independent groups. Per-dataset production stays bounded and retains complete source accounting. |
| C: Complete downstream checks | Relationships, quality, normalization, impacts, and controls finish without whole-run materialization. A row advisory cannot bypass an unqualified stage. |
| D: Qualify the user journey | The motivating workspace shape completes Prepare data, opens Final review, and works after Recipe saving and reuse on fresh files. Publish current documentation only after this succeeds. |

Minimum qualification uses three physical inputs with 65,299 total rows,
262 fields including empty and colliding fields, and at least two mapped
logical datasets. Cover a reviewed combined output beside a directly mapped
table, independent join outputs, and the actual added provider. Include both
incoming relationships and external Many2one aliases, company scope, and
intentional blanks using arbitrary custom model and field names.

Required checks:

1. Compare complete canonical values, lineage, issues, relationship outcomes,
   impacts, control totals, and quality decisions with the authoritative small
   fixture across batch sizes. Preserve readable historic evidence and hashes.
2. Complete the 65,299-row fixture and a 100,000-row fixture through the real
   preparation worker. Trap whole-run fallback in those tests. Include clean,
   error-heavy, wide-field, duplicate-key, and unmatched-key cases.
3. Confirm row count alone produces an advisory and does not stop qualified
   execution. Verify counts around the former 25,000-row and 50,000-row gates.
   Test a run beyond the initial measured range with explicit resource budgets.
4. Inject cancellation, disk-budget exhaustion, and worker failure before and
   during publication. Verify deterministic retries and preservation of the
   previous successful result. Reject stale source, schema, and mapping binds.
5. Measure first and repeat preparation in three fresh worker processes. Report
   phase times, peak worker memory, temporary storage, connection and schema
   counts, physical scans, and reconstruction counts. Qualify macOS for this
   reported workflow and run the portable suite on the supported CI platforms;
   make platform claims only from completed measurements.
6. Run the browser journey with fictional data, including advisory progress,
   cancellation, final review, Recipe reuse, and restart/build-contract checks.
   Run focused tests and the complete repository gate before delivery.

The proposal is complete only when the actual motivating workspace shape
passes the full preparation and review path. A matching dialog, an isolated
join benchmark, or a raised constant cannot establish that outcome.

## Owners to change during implementation

- `application/workspace/preparation/preparation_capability.py` owns graph
  admission and advisories; `preparation_service.py` owns orchestration.
- `bounded_preparation.py` and `grouped_exact_join_preparation.py` provide
  reusable producers through application-owned ports. DuckDB and Polars
  adapters retain storage and execution ownership.
- Preparation sessions, resolution, indexed quality, normalization, and
  canonical publication own the complete output and its recovery boundary.
- Preparation jobs, progress presenters, and templates expose the advisory,
  current phase, and source/output accounting. They do not repeat worker scans.

After implementation, update the owning user and developer workflow pages,
contracts, workflow registry, code map, and affected screenshots. Retire this
plan after that behavior and its evidence are verified.
