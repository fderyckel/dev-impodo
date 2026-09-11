# Smart Match data ordering and early Odoo refinement

## Status and decision

**Status:** Implementation in progress, 2026-09-10. Phases 0, 1, and 2 are
implemented. The explicit live Odoo refinement remains planned for a later
phase.

Stage 3 will present the source tables as a guided matching queue. Impodo will
recommend an order that places supporting tables before the tables that use
them. The data manager may arrange the queue differently without changing the
mapping, Recipe, or eventual Odoo load order.

Impodo will calculate the first recommendation from the Odoo structure already
captured in **Odoo data**, the source preparation rules, and the current saved
matching work. When enough business-key and relationship choices are available,
the data manager may select **Check Odoo and update suggestion**. This explicit
read-only action will check which related records already exist and refine the
recommendation.

The Stage 3 check is advice for completing **Match data**. It is not prepared
evidence, a final comparison, load approval, or write authority. **Final
review** must still repeat the fresh target comparison before Impodo can offer
**Load into Odoo**.

## Outcome

A data manager who receives several related tables will see where to begin,
why one table is recommended before another, and which table to match next.
The same generic dependency meaning that later protects the Odoo load will
produce this guidance. The implementation must not contain a Product, routing,
or bill-of-material-specific sequence.

For example, suppose the source delivery contains `plw_article`,
`plw_bomversion`, `plw_bom`, `plw_route`, and `plw_routeopr`. If the reviewed
relationships show that a bill of material refers to an article, Impodo will
recommend the article table first. If every referenced article already exists
uniquely in the current Odoo database, the optional live check may mark that
dependency as currently satisfied and refine the remaining work order.

For the data manager, this means the queue explains the business sequence
without asking them to understand or maintain a dependency graph. The data
manager can still use their own working sequence. Impodo always calculates the
safe load order again from the reviewed rows and current Odoo state.

## Current behavior and gap

`canonical_mapping_source_selection` currently places mapping datasets in
stable dataset-identifier order. `_mapping_dataset_views` and
`mapping/page.html` retain that order and label each item as **Table N**. The
order is deterministic, but it does not express which table supports another
table.

Stage 2 already captures the Odoo field facts needed for a preliminary
structural recommendation. `SchemaField` records whether a field is required,
whether it is read-only, and which Odoo model it references. A saved mapping
adds the data manager's exact incoming-table resolvers.

The later preflight goes further. It evaluates prepared business keys, reads
current Odoo records in bounded requests, and decides whether a relationship
resolves to Odoo or to another incoming row. Stage 3 does not currently perform
that target-record check. Mapping validation explicitly leaves current target
existence and uniqueness to later evidence.

The planned feature closes the usability gap while preserving that evidence
boundary. It adds progressive guidance to Stage 3 and keeps the final preflight
authoritative.

## Two different orders

| Order | Purpose | Authority |
| --- | --- | --- |
| Matching work order | Helps the data manager complete Stage 3 in a useful sequence. | Impodo recommends it, and the data manager may rearrange it. |
| Odoo execution order | Controls when records are created and relationships are written. | Impodo recomputes and enforces it from reviewed preparation and fresh target evidence. |

Changing the matching work order must not reorder `SourceSelection.datasets`,
`MappingDefinition.datasets`, or `CompiledMigrationPlan.datasets`. It must not
change a mapping content hash, make a Recipe revision different, invalidate
prepared evidence, or influence the safety rules used during execution.

The data manager's order may be used only as a stable tie-breaker in the Stage
3 recommendation when two tables have no known ordering relationship. It must
not be passed to the execution snapshot as a dependency override.

## Data-manager experience

### Open Match data

Place a new **Recommended matching order** card after the Stage 3 heading and
notices and before the current mapping form. The card must remain outside the
large mapping form so its refresh and reorder actions do not submit unsaved
field choices.

The card will contain:

- A heading that says **Match supporting tables first**.
- A short explanation that Impodo derives the suggestion from reviewed
  relationships rather than filenames.
- The freshness and coverage of the suggestion.
- One compact row for every source table.
- An explanation for every table that has a predecessor.
- An **Edit table** action that opens the current table editor.
- A **Reorder tables** action and a **Use Impodo order** recovery action.
- An optional **Check Odoo and update suggestion** action when a read credential
  is available.

Each compact row will show its display position, source table name, selected
Odoo model, current Stage 3 status, and the reason for its position. Use these
status labels:

- **Ready now** means no known unfinished table needs to be matched first.
- **In progress** means a recoverable working draft exists for this table.
- **Ready to check** means the table has the minimum complete choices needed by
  the current semantic validator.
- **Needs attention** means a current issue or unresolved ordering fact requires
  review.

Do not label one table **Confirmed** because Stage 3 confirmation is one
mapping-wide decision. When the current mapping is confirmed, the existing
page status remains the authority.

Change **Table N** in each dataset heading to **Step N of M**. Preserve the
stable `view.index` used by form field names and URLs. Add a separate
`display_position` for the visible sequence.

### Move between tables

Selecting **Edit table** will use the existing `mapping_dataset` query
parameter. The current unsaved-change protection must continue to warn before
navigation. After **Save progress**, the page may offer **Next recommended
table** when another incomplete table remains.

The first delivery will keep the existing collapsed cards for inactive tables.
It will order those cards like the compact queue. It will not make the data
manager drag a full field editor.

### Rearrange the queue

Selecting **Reorder tables** will reveal drag handles and keyboard-accessible
**Move up** and **Move down** actions. A manual change will add a **Custom
order** label. Selecting **Use Impodo order** will restore the newest applicable
recommendation.

The data manager may place a consumer before its supporting table because this
control changes authoring convenience, not execution safety. When the custom
order crosses a known dependency, show an amber explanation such as:

> Impodo recommends matching `plw_article` first because this table refers to
> it.

Do not reject the move or imply that the later Odoo load will follow the custom
sequence.

### Improve the order with current Odoo data

The card will initially say **Based on captured Odoo structure**. When current
saved choices provide enough business identity and relationship information,
enable **Check Odoo and update suggestion**.

The action must be explicit. Opening or refreshing the Match data page must
not contact Odoo. While the check runs, show **Checking current Odoo data** and
keep the mapping editor available unless workspace consistency requires a
short publication lock.

After success, show the target database label, check time, read-only boundary,
and coverage. The normal message will be:

> Checked against Odoo at HH:MM. Read-only. No data changed.

The queue may then explain an ordering fact with one of these outcomes:

- **Required first** means at least one reviewed relationship needs a record
  supplied by another incoming table.
- **Already in Odoo** means every checked key for that relationship currently
  resolves to one existing Odoo record.
- **Mixed source** means some keys exist in Odoo and other keys must come from
  the incoming table. Keep the supporting table before its consumer.
- **Order not known yet** means the required model, business key, source value,
  or incoming resolver is not ready for an exact check.
- **Odoo structure changed** means the candidate metadata no longer matches
  the current Stage 2 evidence. The data manager must return to **Odoo data**
  and review the refresh.

A partial result is useful and must say how many tables and relationships were
checked. It must not present unchecked dependencies as satisfied.

### Apply an updated recommendation

Saving a model, identity, or relationship may change the recommended order.
Impodo must not move table cards while the data manager is editing. Show
**A better order is available** with **Review order** and **Apply
recommendation**. The data manager's current custom order remains until they
apply or reset it.

## Recommendation inputs and confidence

The recommendation service will use evidence in this order:

1. A saved incoming-table resolver is a confirmed authoring dependency.
2. A source preparation parent-child rule is a confirmed structural
   dependency.
3. An Odoo relationship field is a preliminary dependency only when the owner
   table has a selected target model and exactly one source table currently
   maps to the related model.
4. A current Odoo check may classify a confirmed dependency as satisfied,
   incoming, mixed, ambiguous, missing, or unchecked.

Filename resemblance must not create an edge. Labels and technical names may
help the data manager understand a recommendation, but they are not evidence.

Preliminary Odoo-model edges may influence the initial suggestion, but the UI
must label the result **Preliminary**. They must not create a mapping validation
error. Confirmed incoming dependencies retain the current hard or deferrable
meaning from `extract_dataset_dependency_edges`.

## Ordering algorithm

Add one pure dataset-ordering function beside the canonical relationship
dependency extractor. The function will accept dataset identifiers, typed
edges, an optional user tie-break order, and live resolution outcomes. It will
return ordered groups, explanations, completeness, and blockers without
opening a repository or Odoo connection.

The function will:

1. Give every dataset one stable node identified by `dataset_id`.
2. Keep confirmed source and mapping edges separate from preliminary schema
   hints.
3. Remove an ordering constraint only when live evidence proves that every
   checked relationship key is uniquely satisfied by Odoo.
4. Retain an edge when the result is incoming, mixed, missing, ambiguous, or
   incomplete.
5. Calculate strongly connected components before topological ordering.
6. Order independent components by the saved custom order and then by the
   existing stable dataset order.
7. Return a reason for every edge and every grouped component.

An optional cycle will appear as **Match these tables together**. A confirmed
required cycle will appear as **Needs attention** because no truthful linear
order exists. A cycle based only on preliminary schema hints is advice to
review the relationships, not a Stage 3 blocker.

Do not create a second dependency algorithm under the web package. Refactor
the current dataset component ordering behind a shared pure function and keep
regression tests that prove `dependency_ordered_execution_datasets` returns
the same execution order as before this feature.

## Early read-only Odoo check

### Preconditions

The live refinement will operate on the current saved working draft. It must
not use unsaved browser fields. A dataset is eligible only when Impodo can
evaluate the exact target model, target business key, scope, and relationship
key needed for a bounded read.

The check may cover eligible datasets while others remain incomplete. It must
report partial coverage. It must not invent a key, use a display name as an
identity, or select the first target record when several match.

### Read plan

The application service will build one immutable read plan before opening the
connector. It will reuse the existing closed `MetadataRequest` and
`RecordRequest` meanings where they fit. The plan will deduplicate exact
business and relationship keys, merge fields by model, and page requests by
the existing connector limits.

The service may reuse the Stage 3 evaluator to produce local key values from
an eligible working draft. It must not publish Stage 4 prepared records or a
Stage 5 `ReadinessReport`. A malformed or incomplete mapping makes only the
affected dataset ineligible for live refinement.

The reader will use the current `READ` credential and the current captured
company context. Its transport surface remains limited to identity probes,
`fields_get`, and bounded `search_read`. No connector call may occur inside a
source-row loop.

The check will first compare the live target fingerprint and requested field
metadata with the current Stage 2 evidence. If the semantic schema changed,
the service will stop before record classification and publish only a safe
stale-schema result. Match data must not confirm or replace a schema candidate.

### Result and evidence boundary

Create an immutable `MatchingOrderCheck` for each completed attempt. It will
bind:

- The workspace and source-selection hashes.
- The captured schema and governance hashes.
- The working-draft version and content hash.
- The target and read-principal fingerprints.
- The checked dataset and relationship coverage.
- The aggregate target, incoming, mixed, missing, and ambiguous counts.
- The resulting recommendation hash.
- The actor and capture time.

Exact target rows, numeric Odoo identifiers, business-key values, and source
values belong only in protected target-specific evidence. The browser result
will contain dataset-level reasons and counts. Portable mapping and Recipe
evidence must never receive a numeric Odoo identifier.

The check cannot validate or submit a mapping. It cannot satisfy Final review,
create an `ExecutionSnapshot`, enable a writer, or authorize an Odoo change.

## Saved custom order

Create a separate `MatchingOrderPreference` owned by the current workspace.
It will store the ordered dataset identifiers, preference version, bound source
selection hash, actor, and update time.

Do not add the preference to `MappingWorkingDraft` or `MappingDefinition`.
Those objects contain portable mapping meaning. A screen-order preference is
not reusable Recipe meaning.

When the source selection changes, preserve the relative order of dataset IDs
that still exist, remove absent IDs, and append new IDs in the current Impodo
recommendation. Mark the prior live check stale. A source change already
invalidates the semantic mapping through the existing evidence lifecycle; the
preference must not add another semantic invalidation.

## Persistence and publication

Add workspace-engine storage for:

- The current versioned `MatchingOrderPreference`.
- Immutable `MatchingOrderCheck` results and one current pointer.
- The protected snapshot or artifact reference used by each successful live
  check.
- One active-check record that prevents duplicate concurrent Odoo reads.

Publish a check only when the source selection, schema, target, read principal,
and working-draft version still match the values captured before the read. A
concurrent mapping save will make the completed check stale rather than bind it
to newer work.

Keep historical check rows for audit according to the existing workspace
evidence policy. Replacing a current pointer must be transactional. A failed
check must preserve the previous completed result and record a safe failure
state without credentials, raw target rows, or source values.

## Invalidation and freshness

| Change | Matching-order result |
| --- | --- |
| Rearrange the custom queue | Preserve the current recommendation and live check; update only the preference. |
| Save unrelated field-provider work | Recalculate local status; keep a live check only when its working-draft hash is unchanged. |
| Change a target model, business key, scope, or incoming relationship | Mark the recommendation and live check stale. |
| Check Odoo and find no schema change | Publish a fresh advisory result without invalidating Stage 2 or the mapping. |
| Detect a schema change | Preserve current schema evidence, mark the suggestion stale, and direct the data manager to **Odoo data**. |
| Confirm a schema change in Stage 2 | Follow the existing mapping and downstream invalidation rules. |
| Change target identity, read principal, or company access | Retire the current live check. |
| Freeze a new source selection | Reconcile the custom preference and retire the current live check. |

The UI must show **Current**, **Preliminary**, **Partial**, or **Needs refresh**
instead of presenting one undifferentiated recommendation.

## Routes and browser behavior

Add these scoped Stage 3 actions:

- `POST /workspaces/{workspace_id}/mapping/order` saves a complete dataset-ID
  permutation or resets it to Impodo's order.
- `POST /workspaces/{workspace_id}/mapping/order/check` starts one explicit
  read-only refinement against the current saved working draft.
- `GET /workspaces/{workspace_id}/mapping/order/check/{check_id}` returns a
  bounded status projection for the active browser session.
- The existing `GET /workspaces/{workspace_id}/mapping` renders the current
  preference, applicable recommendation, and check status without contacting
  Odoo.

Both POST actions require the authenticated workspace session, same-origin
request policy, CSRF token, and `MAPPING_EDIT`. The check also requires a
current target-bound `READ` credential. Neither route may resolve or accept a
write credential.

Use optimistic preference versions. A stale reorder returns HTTP 409 and keeps
the current saved order visible. One workspace may have only one active live
check. Reopening the page observes that attempt instead of starting another.

The browser must support reordering without drag-and-drop. JavaScript may add
pointer dragging, but server-recognized **Move up**, **Move down**, **Apply
recommendation**, and **Use Impodo order** actions remain the accessible
authority.

## Proposed ownership

| Responsibility | Proposed owner |
| --- | --- |
| Matching-order contracts, groups, reasons, and pure ordering | `src/impodo/domain/relationship_dependencies.py` or a focused `domain/matching_order.py` module |
| Structural and live recommendation orchestration | `src/impodo/application/workspace/mapping/order_service.py::MatchingOrderService` |
| Versioned preference and check persistence | `src/impodo/adapters/duckdb/matching_order_repository.py` |
| Protected target snapshot storage | The existing workspace protected-artifact boundary, through a matching-order-specific port |
| Read-only Odoo composition | `src/impodo/web/composition/target_readers.py` through a narrow matching-order reader factory |
| Stage 3 routes | `src/impodo/web/routers/mapping.py` or a focused router included by it |
| Browser projection | `src/impodo/web/presenters/mapping_view.py` |
| Queue markup | `src/impodo/web/templates/mapping/_matching_order.html` |
| Queue interaction and accessibility | `src/impodo/web/static/mapping-order.js` and `mapping.css` |

`MappingWorkspaceService` remains responsible for portable matching work. The
new service owns advisory ordering and must not grow the mapping contract or
the execution service.

## Delivery phases

### Phase 0: Preserve the execution algorithm

**Implemented, 2026-09-10.** `order_dataset_dependency_components` now owns
the pure dataset graph operation. The existing execution snapshot delegates to
it without changing the reviewed dataset sequence. Focused tests cover input
permutations, cycles, tie-break order, legacy execution behavior, and a
1,500-dataset chain without recursive graph traversal.

- Extract or wrap the current dataset component ordering behind one pure
  domain function.
- Add permutation, cycle, and regression tests before changing browser order.
- Prove that current execution snapshots retain identical dataset sequences.

### Phase 1: Structural recommendation and work queue

**Implemented, 2026-09-10.** `MatchingOrderService` now combines saved
incoming-table resolvers, source-preparation links, and unambiguous captured
Odoo relationship hints. Stage 3 renders the resulting local-only queue,
preserves stable form indexes, explains confirmed and preliminary positions,
and offers the next incomplete recommended table after **Save progress**.
Opening or saving the page does not contact Odoo for this recommendation.

- Build confirmed and preliminary edges from current local evidence.
- Add the ordered compact queue and explanations to Stage 3.
- Preserve stable form indexes while adding display positions.
- Add **Next recommended table** after a successful progress save.
- Do not contact Odoo in this phase.

### Phase 2: Persisted user order

**Implemented, 2026-09-10.** Stage 3 now stores a versioned workspace-local
display preference. The data manager can use accessible one-position actions
or pointer dragging, receives an amber explanation when the chosen order
crosses a known dependency, and can restore Impodo's current order. Stale
writes return HTTP 409 with the latest saved order. The preference has its own
workspace-engine table and audit events; saving or resetting it does not alter
or invalidate mapping, Recipe, preparation, preflight, or execution evidence.

- Add the workspace preference contract, storage, and forward migration.
- Add keyboard and pointer reordering, optimistic conflicts, custom-order
  warnings, and reset behavior.
- Prove that reordering does not change mapping, Recipe, preparation, or
  execution hashes.

### Phase 3: Early read-only Odoo refinement

**Implemented, 2026-09-11.** Stage 3 now offers one explicit, bounded,
read-only Odoo check for the exact governed relationship keys in the saved
working draft. It stores exact keys and target records behind the protected
workspace boundary, publishes only aggregate outcomes, preserves the previous
result on failure or drift, and removes a dependency only for complete unique
target coverage. One durable attempt may be active per workspace; the browser
shows recoverable progress and freshness, while applying a changed suggestion
remains a separate user action.

- Add the bounded read plan, protected result, single active attempt, and
  partial coverage rules.
- Add the explicit check action, progress state, failure recovery, freshness,
  and apply-recommendation flow.
- Prove that the route cannot resolve a write credential or publish later-stage
  evidence.

### Phase 4: Documentation and visual qualification

- Update the paired Match data user and developer pages after implementation.
- Register new owners and tests in `docs/workflow.yml`.
- Update the Python code map and evidence lifecycle contract.
- Capture an authenticated 1440 by 1024 screenshot with fictional related
  tables, a recommendation explanation, and one custom-order warning.
- Run the complete Match data, relationship, preflight, security, and
  documentation checks.

## Verification plan

### Domain tests

- The same nodes, edges, and tie-break order always produce the same result.
- A dependency always appears before its consumer in Impodo's recommendation.
- Independent tables follow the saved preference.
- A target-satisfied edge may be removed only with complete unique evidence.
- Mixed, missing, ambiguous, and unchecked results retain the conservative
  ordering edge.
- Optional cycles form one match-together group.
- Required cycles produce an explained review state.
- Technical-name resemblance alone never creates a dependency.

### Application and adapter tests

- A partial working draft produces a partial recommendation without inventing
  missing keys.
- Record requirements are deduplicated and grouped by model.
- Request count follows model and page count, not source-row count.
- A concurrent mapping save prevents publication of the completed check.
- A failed or cancelled read preserves the previous completed result.
- Preference writes enforce expected versions and complete permutations.
- Source-selection changes reconcile the preference deterministically.
- Protected snapshots never enter portable mapping or Recipe JSON.
- Workspace schema upgrades create the new tables transactionally.

### Browser tests

- Stage 3 shows the ordered queue, reason text, status, and current table.
- Page GET performs no Odoo call.
- **Edit table** preserves the existing active-dataset navigation.
- Dirty mapping work receives the existing navigation warning.
- Pointer and keyboard reordering produce the same saved preference.
- A known dependency crossed by a custom order shows an amber explanation.
- **Use Impodo order** restores the current suggestion.
- The live check requires CSRF, same-origin policy, authorization, and a
  current read credential.
- The progress, partial, complete, stale-schema, missing-credential, timeout,
  and conflict states remain recoverable.
- No normal browser response contains source values, target keys, numeric Odoo
  identifiers, credentials, or raw connector errors.

### Execution regression tests

- Existing dependency-order fixtures produce byte-equivalent execution dataset
  sequences after Phase 0.
- A custom matching order cannot alter a compiled plan or execution snapshot.
- Final review performs its own current target read after any Stage 3 advisory
  check.
- The execution journal and load confirmation remain unavailable until the
  existing final evidence is current.

### Performance qualification

- Measure structural recommendation time for 1, 10, 100, and 1,000 datasets.
- Measure the live check at the current direct, relationship, and derived row
  limits with representative duplicate relationship keys.
- Assert bounded request pages and one identity probe per required model set.
- Assert no DuckDB query, source scan, connector call, or Python callback per
  relationship row.
- Confirm that an active check does not block read-only navigation or grow an
  unbounded worker queue.

## Acceptance criteria

The feature is complete when:

1. Stage 3 opens with a deterministic, explained recommendation from current
   local evidence and makes no hidden Odoo call.
2. The data manager can open tables in that order, rearrange them with pointer
   or keyboard controls, and restore Impodo's recommendation.
3. A custom order remains an authoring preference and cannot change portable
   mapping meaning or execution safety.
4. The explicit live check uses only bounded read operations and reports honest
   partial coverage.
5. Schema or target drift makes the advisory result stale and directs the data
   manager to the existing recovery stage.
6. A changed recommendation never silently moves the active editor.
7. Final review and Load into Odoo repeat and enforce their existing fresh
   comparison, approval, journal, and verification boundaries.
8. Focused domain, persistence, browser, security, performance, execution
   regression, and documentation checks pass.

## Related documentation

- [Match data user workflow](../user/workflow/03-match-data.md)
- [Match data developer workflow](../developer/workflow/03-match-data.md)
- [Workflow evidence lifecycle](../developer/contracts/evidence-lifecycle.md)
- [Preflight contract](../developer/contracts/preflight.md)
- [Scalable relationship dependency planning](scalable-relationship-dependency-planning.md)
- [Security and infrastructure](../architecture/security-and-infrastructure.md)
