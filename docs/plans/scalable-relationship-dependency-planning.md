# Scalable relationship dependency planning and execution

## Status and authority

**Status:** Accepted implementation plan, revised 2026-08-28. Phases 0 through
5 are complete, and Phase 6 is next. The remaining plan is not current
browser behavior and does not authorize a Production load.

The current implementation orders related datasets and rows before their
consumers, rejects actual required-at-create row cycles, applies only the
planned optional cyclic relationships in a second write pass, binds the row
schedule to the execution snapshot, and stops before target I/O when a row
dependency cannot be scheduled. Execution now revalidates existing targets in
bounded crosswalk pages, consumes bounded component pages, and requires a
journalled create receipt before a dependent write.
Every Odoo transport batch is now journalled as in flight before the call.
After a process interruption, exact read-back must prove earlier components
and classify the interrupted rows before the same run can resume.

This plan extends the current generic relationship contract. It does not add
special executor branches for Products, units of measure, categories, bills of
materials, or another Odoo model. The captured Odoo 19 schema and the compiled
mapping remain the authorities for every relationship.

## Outcome

When one migration contains several related record types, Impodo will produce
one deterministic, reviewable dependency plan before the data manager confirms
the load. The plan will place every resolvable dependency before its consumer,
use a second write pass only for a genuine optional cycle, and stop before the
first Odoo write when a required dependency cannot be satisfied safely.

The same mechanism must support a small Product migration and a larger shape
such as:

```text
Units and categories
`-- Products and variants
    `-- Bills of materials
        |-- Component lines
        |-- Operations and their supporting records
        `-- By-products
```

This tree illustrates business order only. The planner must derive the exact
Odoo models, fields, and required-at-create behavior from the current captured
schema. It must not encode this example as a fixed BOM sequence.

## Non-goals

This work does not:

- make an unsupported Odoo model or business operation loadable;
- infer relationships from names when the data manager has not reviewed a
  business key;
- put target database identifiers into a portable Recipe;
- weaken **Check changes**, **Confirm and load**, or **Verify result**;
- retry an Odoo call whose outcome is unknown;
- provide whole-migration rollback; or
- raise the current related-data row limit without separate scale
  qualification.

## Current baseline and open gaps

| Concern | Current behavior | Planned result |
| --- | --- | --- |
| Dataset order | `dependency_ordered_execution_datasets` places dependency components before consumers. | Preserve this early, compact graph and make its evidence explicit. |
| Required cycles | Mapping and compiled-profile validation reject required-at-create dataset cycles. | Keep cross-dataset rejection, but decide same-dataset dependencies from actual rows so an acyclic hierarchy can be ordered. |
| Optional cycles | Execution creates records without unresolved optional fields, then patches those fields. | Restrict the second pass to edges that are members of a proven cycle or that cross an incomplete component. |
| Same-dataset order | The dataset graph cannot place one row before another row in the same dataset. | Freeze a deterministic row schedule and load parent rows before child rows. |
| Relationship resolution | Prepared evidence keeps incoming logical references; target matches must be unique. | Convert every eligible incoming reference into one auditable row edge and every target match into one satisfied dependency token. |
| Progress | Load progress distinguishes initial writes, relationship work, and verification. | Report scheduled components and relationship work without exposing graph internals in the normal user path. |
| Scale | Preparation has set-based relationship evidence, but execution planning is not qualified for the 16,000-Product and 80,000-BOM-line shape. | Use compact graph arrays, bounded snapshot publication, bulk target resolution, and an explicit Windows qualification gate. |

## Design principles

1. **One generic rule owns every relationship.** A compiled incoming
   relationship creates an edge. An exact existing-target match creates a
   satisfied dependency. Missing or ambiguous evidence creates a blocker.
2. **Business identities define meaning.** Recipe and prepared evidence use
   business keys and scope. Target-bound execution may use a reviewed Odoo
   identifier or Impodo External ID, but that value never becomes portable
   Recipe meaning.
3. **The plan is immutable before approval.** The schedule, dependency
   summary, target evidence, and row intentions contribute to one semantic
   hash. A changed edge or order requires **Check changes** again.
4. **Acyclic work has one write pass.** An acyclic incoming graph must produce
   no deferred relationship update merely because the source datasets arrived
   in an inconvenient order.
5. **True optional cycles are explicit.** Impodo creates the involved rows
   without the deferrable fields, journals their identities, and then finishes
   only those fields.
6. **Required cycles fail before transport.** Impodo does not insert dummy
   values, depend on an Odoo default, or temporarily violate a required field.
7. **Every target call is bounded.** The planner and executor must not perform
   one schema read, target lookup, permission check, or read-back call per
   relationship.
8. **Recovery uses durable evidence.** A restart resumes only from journalled
   results and fresh target read-back. It never reconstructs success from a
   progress percentage.
9. **The data manager keeps business choices.** The data manager chooses the
   participating datasets and rows, the reviewed business keys, and whether an
   optional field is mapped. Impodo calculates the safe order from those
   choices instead of asking the data manager to maintain a graph.

## Operator choices and safe automation

The data manager remains free to revise the source selection, relationship
mapping, business key, optional field coverage, or set-aside rows and then run
**Check changes** again. Each revision produces a new reviewed snapshot. The
planner does not silently carry an earlier approval into the revised work.

Impodo owns the mechanics that should not depend on manual judgment. It orders
dependencies, detects cycles, revalidates reviewed target matches, and chooses
the safe point for an optional relationship. The browser does not ask the data
manager to drag graph nodes into order or override a required Odoo dependency.

When one blocked branch should not participate in the load, the data manager
sets aside the affected source work before **Check changes**. The first delivery
does not add an executor override that loads an unreviewed subset of a frozen
snapshot. A future partial-load workflow would create and approve its own
snapshot rather than weakening the current confirmation.

## Proposed ownership and evidence

The relationship plan belongs to the current `ExecutionSnapshot`. It is not a
new Project, Recipe, DataVersion, or MigrationRun aggregate.

Extend the existing `ExecutionSnapshot` contract instead of creating another
aggregate or store in the first delivery. Each scheduled `ExecutionRow` records
its deterministic ordinal and component. Each relational `FieldIntent` records
the resolved dependency row identifiers and whether execution writes the field
in the first pass or finishes it after a cycle has been broken. The snapshot
also records the relationship-plan contract version, row and edge counts,
target-crosswalk hash, and schedule root hash.

`RelationshipEdge` and `RelationshipComponent` remain pure domain concepts
used while Impodo calculates the schedule. The snapshot persists the resolved
dependencies and exact completion fields that execution needs. It does not
duplicate scalar prepared values or create an alternate prepared-data store.

Phase 0 measures snapshot size, planning memory, and edge cardinality before
this contract is implemented. If the current related-data limit cannot be
served safely by the snapshot-owned representation, a later reviewed decision
may move only the large row relations to hash-bound sidecar Parquet artifacts.
The plan does not add that storage boundary speculatively.

Do not update an older snapshot in place. Advancing the relationship-plan or
execution-snapshot contract makes older pending previews stale and sends the
data manager back to **Check changes**. This is a normal contract upgrade, not
a compatibility rewrite or monkey patch.

## Proposed code placement

| Responsibility | Proposed owner |
| --- | --- |
| Snapshot-owned schedule, edge references, and component contracts | `src/impodo/domain/execution_snapshot.py` |
| Pure dataset and compact row scheduling algorithms | `src/impodo/domain/execution/dependency_scheduler.py` |
| Frozen-evidence orchestration and snapshot publication | `src/impodo/application/preflight_service.py::PreflightService` |
| Current snapshot pointer and transactional publication record | `src/impodo/adapters/duckdb/preflight_repository.py` |
| Frozen schedule consumption and journal transitions | `src/impodo/application/workspace/execution/service.py::ExecutionService` |
| Bounded Odoo 19 write operations and receipt contracts | `src/impodo/domain/execution/odoo_write.py` and the matching adapter |
| Component and relationship progress projection | `src/impodo/application/workspace/execution/load_jobs.py::LoadJobManager` |

Keep graph algorithms in the domain package. They accept immutable scalar
contracts and return immutable scalar contracts; they do not open DuckDB,
Parquet, a workspace, or Odoo. The application service owns materialization,
atomic publication, current-evidence checks, and transaction boundaries. The
adapters own storage and transport details.

Do not grow `ExecutionService` into the graph builder. It should validate and
consume a published plan. Do not create an alternate BOM planner under an
adapter or web route.

## Graph model

### Dataset graph

The compiler builds a node for each compiled dataset. It adds a scheduling edge
from each incoming dependency dataset to the consumer dataset that uses it in:

- a target identity component;
- target scope;
- a many-to-one or many-to-many field; or
- another supported relational field with exact replacement semantics.

This graph remains small enough for an in-memory strongly connected component
calculation. Its stable order uses the reviewed dataset order only as a tie
breaker. A dependency always wins over that original order.

The compiler records two edge classes:

- A **hard edge** is part of identity or scope, or the captured contract says
  that the relationship is required when Odoo creates the owner row.
- A **deferrable edge** is an optional relationship that Odoo allows Impodo to
  omit during create and set later.

Required cross-dataset cycles remain invalid at mapping confirmation. A
self-reference is retained for row-level validation instead of being rejected
only because its owner and target share one dataset.

### Row graph

After preparation and target comparison, the planner builds a node for each
eligible `ExecutionRow`. It resolves each incoming `LogicalReference` through
the unique incoming business-key index and emits one relationship edge per
referenced row. The scheduler interprets that edge from the referenced target
to its owner. Many-to-many values emit one edge for every unique referenced
member.

The node also records whether its target record:

- already exists and has a reviewed Odoo identity;
- will be created in this execution;
- will be updated in this execution; or
- will not be written because it is unchanged or blocked.

An existing, uniquely resolved target record satisfies dependencies before
transport. A new record satisfies dependencies only after its create receipt
is durable. A blocked target propagates one actionable blocker to every
dependent row without repeatedly walking the same fan-out.

The planner must assert that every incoming reference in an eligible row
produces exactly one edge. An omitted, duplicated, or unowned reference is a
planning error, not a reason to fall back to the current source order.

### Deterministic scheduling

The scheduler performs these steps:

1. Assign stable dense integer node identifiers without loading complete
   prepared records into Python. The sort key is dataset sequence, canonical
   business identity, canonical scope, and `row_id`.
2. Store adjacency as compact integer arrays. The graph memory therefore grows
   with nodes and edges, not with the width of source rows.
3. Use Kahn's algorithm with a stable ready queue to schedule all acyclic
   dependency edges, including optional edges that do not participate in a
   cycle.
4. Calculate strongly connected components only for the unresolved full graph.
   Use an iterative traversal for the row graph so a deep hierarchy cannot
   exhaust the Python call stack.
5. Remove dependencies that existing target records already satisfy, then
   reject an unresolved component only when its hard-edge subgraph is cyclic.
6. Choose a deterministic order that respects every remaining hard edge. Defer
   only optional owner fields whose dependencies point backwards in that
   order, and group each owner's deferred values into one completion write
   when their writable shape permits it.
7. Hash the ordered schedule and its edge evidence into the snapshot.

The planner promises a deterministic and exact completion list for the chosen
order. It does not claim to solve the global minimum feedback-edge problem.
Later optimization may reduce completion writes without changing which source
relationships the data manager approved.

The dataset graph may keep its current recursive strongly connected component
implementation because its node count is bounded by the number of mapped
datasets. The row graph must not reuse that recursive implementation.

## Cycle policy

| Component | Result before confirmation | Execution behavior |
| --- | --- | --- |
| Acyclic hard or optional edges | Ready when every target is unique and eligible. | Write once in dependency order. |
| Optional cycle among new rows | Ready with a visible relationship-completion count. | Create rows without only the cyclic fields, journal receipts, then patch those fields. |
| Cycle among rows that all already exist | Ready when every existing identity is unique. | Write reviewed relationships directly because all target identifiers already exist. |
| Cycle whose hard-edge subgraph remains cyclic and contains a new row | Blocked. | No Odoo write occurs. The data manager must change the source, mapping, or supported operation. |
| Missing, ambiguous, quarantined, or excluded target | Blocked with the root cause and affected count. | No dependent write occurs. |
| Unsupported relation operation | Blocked. | No generic fallback or incremental command is sent. |

For a component that mixes new and existing records, the planner removes
already satisfied existing nodes before it decides whether a true create cycle
remains. This avoids a false cycle caused only by rows that Odoo already owns.

## BOM-shaped example

Suppose the accepted data contains units, product templates, product variants,
BOM headers, component lines, and optional operations. The compiled mapping
may produce these dependencies:

```text
unit ------------> product template
category --------> product template
product template -> product variant
product template -> BOM header
product variant --> BOM header
BOM header ------> component line
product variant --> component line
work centre -----> operation
BOM header ------> operation
```

Impodo calculates this graph from reviewed relationships. If the source file
lists component lines first, the resulting schedule still loads their product
and BOM dependencies first. If a multi-level BOM uses one finished product as
a component of another BOM, the row graph orders the relevant product and BOM
rows from their actual keys.

The example deliberately does not define a universal Odoo BOM schema. The
captured Odoo 19 metadata decides whether a field is available, writable,
required during create, company-scoped, or outside the permitted operation.
Unsupported operations stay blocked even when their graph is acyclic.

## Planning lifecycle

The owning application flow becomes:

```text
Confirm field matches
  -> compile dataset dependencies
Prepare data
  -> resolve incoming and existing-target business keys
Final review / Check changes
  -> build and persist the row dependency plan
  -> validate blockers, cycles, permissions, and target evidence
  -> freeze the plan hash in the ExecutionSnapshot
Confirm and load
  -> revalidate all hashes
  -> execute scheduled components
  -> finish approved optional relationships
Verify result
  -> read back affected records in bounded model groups
```

### Entry conditions

The planner requires:

- one current compiled mapping and captured Odoo 19 schema;
- one immutable prepared and quality result;
- unique incoming business identities;
- current target-comparison and relationship-resolution evidence;
- explicit write dispositions for every row; and
- the current permitted model and field scope.

Final review resolves existing-target keys in bounded model-and-key groups.
The protected, target-bound plan may retain the exact reviewed Odoo identifiers
needed for execution. Those identifiers never enter a Recipe or another
target's plan. Immediately before a write run starts, Impodo resolves the same
keys again in bounded groups and requires the resulting unique identifiers to
match the frozen crosswalk. This check detects a missing, duplicated, or
retargeted business key without recalculating the approved schedule.

### Exit conditions

Planning is complete only when:

- every eligible incoming logical reference has one edge;
- every edge is satisfied, scheduled, deferred by an accepted optional cycle,
  or represented by a blocker;
- every scheduled row appears exactly once in the first pass;
- every deferred field appears exactly once in the completion pass;
- all required-at-create dependencies precede their owners;
- graph, schedule, snapshot, target, permission, and credential bindings agree;
  and
- the exact schedule root hash is visible to execution.

### Invalidation

Any change to source evidence, selected datasets, mapping, Recipe revision,
prepared rows, business keys, relationship rules, target schema, target
records used for resolution, company context, permissions, write disposition,
or dependency order invalidates the relationship plan and the current load
confirmation.

## Execution contract

The executor consumes the frozen schedule. It must not recalculate a different
order after confirmation. Before it creates the write journal, a read-only
crosswalk verifier rechecks the frozen existing-target identities in bounded
model-and-key groups. A mismatch sends the data manager back to **Check
changes** before the first Odoo write.

For each component it:

1. Loads the successfully revalidated frozen target-bound identity crosswalk.
2. Commits planned row attempts before write transport.
3. Creates compatible new rows in bounded groups. It omits only the exact
   fields named by the component's accepted optional-cycle plan.
4. Journals each returned Odoo identifier or External ID receipt before a
   dependent component can run.
5. Sends existing-row updates after every new target they reference has a
   durable receipt.
6. Applies the component's relationship-completion pass, if any.
7. Stops all later work after an unknown outcome.
8. Publishes progress from journal evidence and saved component state.

The executor may batch records only when they share the target model,
operation, writable field shape, company context, and dependency readiness.
Batching must preserve row-level attempt evidence. It must never merge records
across a boundary where one record depends on another record in that same
batch unless the Odoo 19 operation has an explicitly tested atomic contract
for that reference shape.

## Failure and recovery

- A deterministic planning failure creates no `ExecutionRun` and performs no
  Odoo write.
- A known Odoo rejection records the affected row or batch and blocks its
  dependants. Independent later components do not continue in the first
  delivery because the reviewed whole-run outcome has changed.
- An unknown outcome records the component and transport batch, stops all
  later calls, and requires target read-back before any retry.
- A restart reads the journal and immutable relationship schedule from the
  `ExecutionSnapshot`. It may
  resume only after reconciliation proves every earlier component's state.
- A changed target or stale snapshot cannot resume. The data manager returns
  to **Check changes** and creates new evidence.
- Relationship completion remains `PARTIALLY_APPLIED` until the exact deferred
  fields are verified. The UI must not report a created row as complete while
  its reviewed relationships remain unfinished.

## Performance and N+1 controls

### Planner

- Extract relationship edges set-wise from current prepared evidence.
- Join incoming references to one unique business-key index per dataset.
- Keep only integer node identifiers and edge metadata in graph memory. Do not
  retain complete `PreparedRecord` or `ExecutionRow` objects in adjacency
  structures.
- Sort and publish snapshot-owned schedule evidence in bounded pages when the
  current storage boundary supports streamed publication.
- Measure node count, edge count, maximum fan-out, maximum depth, strongly
  connected component sizes, planning wall time, and peak worker memory.
- Fail closed at the configured graph limit. Do not silently switch to an
  unqualified per-row path.

### Odoo reads

- During final review, combine relationship keys by model, identity-field
  shape, scope, and company context before contacting Odoo.
- Use bounded domain chunks and request only the identity and reviewed fields.
- Build one crosswalk from the returned records. Do not call `search_read`,
  `name_search`, permission inspection, or schema inspection for each row.
- Treat archived, company-incompatible, missing, and ambiguous matches as
  explicit resolution outcomes.

### Odoo writes and verification

- Preserve bounded creates grouped by compatible field shape.
- Replace the current one-relationship-update-per-row path only when a
  measured, Odoo-19-compatible batch operation preserves exact journalling and
  unknown-outcome handling. Until that operation is proven, its current N+1
  behavior remains a declared scale blocker for update-heavy or large cyclic
  workloads.
- Report each Odoo call and relationship-completion count so a 1,001-row run
  cannot appear stuck after its first-pass records were sent.
- Reconcile by model and bounded field scope. Do not read back one row at a
  time.

No throughput claim is accepted from algorithm inspection alone. The release
report must include actual request counts, wall time, peak memory, artifact
size, and repeat-run evidence.

## Odoo 19 and security boundaries

- Remote transport remains the scoped Odoo 19 JSON-2 adapter. The planner has
  no write capability.
- The captured schema determines relational type, required-at-create behavior,
  writable fields, related model, and company context.
- The writer accepts only the models, fields, operation shapes, and target
  fingerprint frozen by the current preview.
- Read and write credentials remain separate capabilities. Relationship
  planning uses read evidence; execution resolves the exact write-role
  credential only after confirmation.
- The planner never calls arbitrary Odoo model methods and never writes
  directly to PostgreSQL.
- Support details may expose stable issue codes, model and field names, counts,
  and evidence identifiers. They must not expose credentials or unrestricted
  source values.

## User experience

The normal path stays compact. **Check changes** shows the business order and
one summary such as:

> Impodo will load 2 supporting record types before Products, then load BOM
> headers before 80,000 component lines. It will finish 12 optional
> relationships after those records exist.

When the plan is safe, the data manager has one obvious next action:
**Confirm and load**. Technical graph details remain under **Support details**.

When the plan is blocked, the page groups root causes instead of listing the
same failure for every dependant. Each group states:

- what supporting record is missing, ambiguous, or in a cycle;
- which record type owns the problem;
- how many rows are affected; and
- the one place where the data manager can resolve it.

Load progress uses four plain phases: **Checking dependencies**, **Sending
records**, **Finishing relationships**, and **Verifying result**. A completed
first pass does not display 100 percent while relationship work is pending.

## Delivery plan

### Phase 0: freeze the contract and baseline

Repair the existing relationship benchmark against the current workspace
contract. Document representative dependency shapes and capture current call
counts, dataset-planning time, load time, relationship patches, snapshot size,
and peak memory. Include a small Product/unit case, a same-dataset parent
hierarchy, an optional cycle, and the existing Product/BOM fixtures.

**Exit result:** accepted fixtures and current measurements make regressions
visible. No runtime behavior changes.

**Completed 2026-08-28:** The accepted fixtures, exact call-count assertions,
fresh-process measurements, and limitations are recorded in the
[Phase 0 dependency baseline](../reports/scalable-relationship-phase-0-baseline-2026-08-28.md).

### Phase 1: complete compiler dependency evidence

Create one shared dependency extractor used by browser mapping compilation,
profile compilation, preflight, and execution-snapshot construction. Record
hard versus deferrable edges. Retain self-references for row-level analysis and
continue to reject required cross-dataset cycles.

Remove duplicate graph interpretations after semantic parity tests pass. Do
not keep old and new planners selected by workspace version.

**Exit result:** every compiled incoming relationship has one canonical
dependency meaning, and permutation tests prove that source dataset order
cannot change it.

**Completed 2026-08-28:** Browser mapping validation, compiled profiles,
preflight evidence, and execution-snapshot construction now use one immutable
edge extractor. The extractor classifies hard and deferrable edges, retains
self-references, and rejects only hard cycles that cross datasets. The
[Phase 1 dependency-contract report](../reports/scalable-relationship-phase-1-dependency-contract-2026-08-28.md)
records the implementation and parity evidence.

### Phase 2: add snapshot-owned row scheduling and cycle classification

Build the compact row graph, deterministic Kahn schedule, iterative unresolved
component calculation, blocker propagation, and deterministic completion plan.
Store the resulting ordinals, resolved dependency row identifiers, components,
completion fields, counts, and root hash in `ExecutionSnapshot`. Allow acyclic
same-dataset hierarchies and reject actual hard-edge row cycles.

**Exit result:** parent rows precede child rows, acyclic fixtures use zero
relationship patches, optional cycles have an exact completion list, and
required cycles stop before Odoo. **Check changes** and execution read the same
immutable, tamper-evident snapshot.

**Completed 2026-08-28:** `ExecutionSnapshot` contract version 5 now stores
row ordinals, component numbers, resolved dependency row identifiers, edge and
component counts, exact completion fields, blocker evidence, and a schedule
root hash. The iterative scheduler orders acyclic same-dataset hierarchies,
cuts only the optional fields needed to break a cycle, propagates blockers,
and rejects hard row cycles. Execution consumes the frozen component layers,
so the hierarchy fixture now uses zero relationship patches. The
[Phase 2 row-scheduling report](../reports/scalable-relationship-phase-2-row-scheduling-2026-08-28.md)
records the implementation and verification evidence.

### Phase 3: integrate execution and bounded crosswalks

Extend `ExecutionService`'s current in-memory component consumption to bounded
component pages. Journal receipts between components, bulk-resolve target
identities, preserve bounded compatible creates, and keep running only planned
completion fields. Preserve the existing stop-on-unknown behavior.

**Exit result:** the execution call sequence equals the approved schedule, no
dependent write precedes its receipt, and the executor performs no per-row
target lookup.

**Completed 2026-08-28:** `ExecutionSnapshot` contract version 6 carries
opaque existing-record bindings without portable numeric Odoo IDs. Before it
opens the execution journal, `ExecutionService` collects every existing row
and target relationship key, resolves those keys in model-grouped pages of at
most 100, and requires each key to resolve uniquely to the record reviewed in
preflight. It then consumes topological components in pages of at most 500
rows. Every retained incoming create edge passes an explicit journalled-receipt
barrier before the dependent call; only frozen optional completion fields run
later. The [Phase 3 execution report](../reports/scalable-relationship-phase-3-bounded-execution-2026-08-28.md)
records the implementation and verification evidence.

### Phase 4: implement recovery and reconciliation by component

Persist the active component and transport batch, classify partially applied
relationship components, and require read-back before resume. Reconciliation
groups reads by model and field scope and proves the schedule's intended final
values.

**Exit result:** process restart, known rejection, unknown outcome, and partial
relationship completion each have deterministic evidence-backed recovery.

**Completed 2026-08-28:** Each attempt stores its schedule component,
component page, global transport batch, and create, update, or completion
phase inside the existing durable row journal. A process restart reloads an
exact `IN_FLIGHT` batch. `ReconciliationService.assess_recovery` reads a
running execution without publishing a final result, groups exact requested
field scopes, and proves the schedule's final scalar and relationship values.
`ExecutionService.resume` atomically records the recovery-report hash across
the run, revalidates the target crosswalk, and retries only an absent create,
an exact update whose reviewed fields still differ, or the frozen deferred
fields of a partially applied create. A known rejection stops independent
later components. The [Phase 4 recovery
report](../reports/scalable-relationship-phase-4-recovery-and-reconciliation-2026-08-28.md)
records the implementation and verification evidence.

### Phase 5: expose progressive user guidance

Add the compact dependency summary, grouped blocker messages, accurate
relationship progress, and bounded support details. Update the paired current
workflow pages and capture new screenshots only after the browser behavior is
implemented.

**Exit result:** a data manager can explain what loads first, why the plan is
blocked, and what to do next without reading graph terminology.

**Completed 2026-08-28:** **Check changes** now derives at most five numbered
load groups and five grouped actionable blocker categories from the immutable
execution snapshot. The presentation stays bounded, hides row identifiers and
source values, and explicitly preserves the data manager's freedom to change
mappings, rows, keys, and optional relationships before comparing again. Load
progress now follows durable journal states, names the current load group, and
does not count in-flight, retry-ready, or relationship-pending rows as final.
The [Phase 5 progressive-guidance
report](../reports/scalable-relationship-phase-5-progressive-guidance-2026-08-28.md)
records the implementation and browser evidence.

### Phase 6: qualify representative scale and Odoo 19 behavior

Run the exact worker and disposable Odoo 19 paths with clean revisions. Test a
16,000-Product and 80,000-BOM-line relationship shape plus representative
multi-level dependencies. Repeat on Windows using the existing related-data
qualification protocol.

**Exit result:** correctness, determinism, request-count, wall-time, memory,
artifact-size, restart, and read-back gates pass. Only then may a separate
decision raise the current related-data limit.

## Verification matrix

### Domain and property tests

- Random permutations of datasets and rows produce the same schedule hash.
- Every eligible incoming `LogicalReference` produces exactly one edge.
- A missing or duplicate incoming business key blocks the owner and all
  dependants once.
- Acyclic chains, diamonds, fan-out, fan-in, and deep hierarchies load in
  dependency order with no deferred write.
- Optional two-node and multi-node cycles produce the deterministic exact
  completion list for the chosen order.
- Required cycles, including a self-cycle, fail before execution.
- Existing-target rows remove false create cycles.
- Many-to-many duplicate members create one edge and one final member.
- Different batch sizes produce identical plan and final-state hashes.

### Application and repository tests

- The planner publishes the schedule with the readiness snapshot atomically.
- A missing or changed schedule invalidates the preview.
- A new plan contract version does not rewrite an older snapshot.
- Journal records exist before every transport call.
- The executor never observes an order other than the frozen schedule.
- Bounded pre-write revalidation rejects a crosswalk whose unique Odoo
  identifiers changed after **Check changes**.
- Known rejection, unknown outcome, cancellation, restart, and reconciliation
  preserve the last durable component.
- The preview and job status use bounded repository reads.

### Odoo 19 integration tests

- Unique existing relationships use the reviewed target record.
- New supporting rows are created before their consumers.
- Optional cycles create first and finish relations second.
- Required, readonly, company-incompatible, archived, missing, ambiguous, and
  unsupported relationship cases fail with stable outcomes.
- Remote External IDs and local returned identifiers produce the same semantic
  result.
- Each target lookup is batched by model and key shape; request counts do not
  grow one-for-one with input rows.
- Reconciliation verifies the exact final relationship values.

### BOM acceptance shape

The acceptance fixture must contain at least:

- products that share units and categories;
- product templates and variants where both are supported by the captured
  schema;
- BOM headers with several component lines;
- the same component reused by many BOMs;
- a multi-level dependency;
- one missing component, one ambiguous key, and one quarantined supporting
  row;
- one optional cycle fixture outside the BOM business data; and
- enough rows to expose N+1 target reads, writes, or read-back.

The test must assert exact connector-call classes and upper bounds. A wall-time
result without request-count evidence cannot qualify the implementation.

## Rollout and completion criteria

Ship the work behind contract-version acceptance, not a runtime feature flag
that leaves two semantic planners active. Each phase must keep older completed
execution and reconciliation evidence readable. Pending incompatible previews
become stale and require **Check changes** again.

The plan is complete only when:

- one dependency extractor serves browser and profile authoring;
- dataset and row schedules are deterministic and immutable;
- acyclic work performs no relationship-completion write;
- optional cycles show and execute the exact deterministic completion list;
- required cycles and unresolved targets stop before Odoo;
- target resolution and read-back have no per-row connector loop, and every
  remaining per-row write path is either removed or excluded by an explicit
  measured scale gate;
- interrupted work resumes only from durable journal and reconciliation
  evidence;
- Product, hierarchy, and BOM-shaped acceptance fixtures pass; and
- current user, developer, contract, architecture, workflow registry,
  screenshots, and acceptance documentation describe the implemented result.

## Related documentation

- [User workflow: Load into Odoo](../user/workflow/06-load-into-odoo.md)
- [Developer workflow: Load into Odoo](../developer/workflow/06-load-into-odoo.md)
- [Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md)
- [Match data developer workflow](../developer/workflow/03-match-data.md)
- [Acceptance and test strategy](../testing/acceptance.md)
- [Phase 0 dependency baseline](../reports/scalable-relationship-phase-0-baseline-2026-08-28.md)
- [Impodo remaining work](remaining-work.md)
