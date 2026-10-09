---
audience: developer
stage: source
status: current
---

# Source data

## Responsibility

Source data converts either registered files or a bounded Odoo selection into
immutable DataVersion evidence. The DataVersion package owns inspection
catalogues, confirmed configuration, selection, freezing, Odoo-source capture,
and snapshot references. A workspace owns only its selected dataset references
and current transformation evidence.

It does not apply the final mapping, publish canonical staging, or perform an
Odoo write.

## Entry conditions

The workspace is `REGISTERED`. File mode requires contained registered files.
Odoo mode requires a captured eligible schema before the bounded record
selection can be saved and frozen. Mapping, summary, and preparation routes
require the derived source-stage readiness result. They must not infer
readiness from a retained mapping submission or another downstream pointer.

## Implementation flow

For file mode, `workspace_setup.py` registers the selected files and invokes
the initial source inspection in the same **Use these files and continue**
request. It then opens the source preview. `sources.py` retains the explicit
recheck route and invokes `SourceWorkspaceService` to save per-file
configuration and freeze the selected tables. The frozen source snapshot is
built from hash-checked CSV or XLSX content and materialized as tagged Parquet
evidence.

`MigrationWorkspaceStateRepository` records each uploaded file in the draft
DataVersion package immediately. `DataVersionOwnedSourceRepository` reads and
writes catalogues and confirmations through that package. The workspace-engine
tables retained for snapshot creation and invalidation are derived caches; a
page never treats them as source-package authority.

File validation and inspection run in spawned, resource-bounded processes.
Each process receives the application build contract that accepted the parent
request and verifies it before opening the source file. A changed editable
installation therefore requires an Impodo restart instead of combining two
builds in one intake operation.

After every file is confirmed, the Source data page uses the already loaded
catalogues and configurations to render the dataset-name fields and the final
save action. `POST /workspaces/{workspace_id}/datasets/freeze` retains the hard
evidence boundary. Before a selection exists,
`GET /workspaces/{workspace_id}/datasets` returns the data manager to the inline
form; after the save, that route presents the saved-table result and the next
action.

For Odoo mode, schema capture occurs first. The source routes then save one
bounded capture selection for each selected model. A model-keyed current
pointer lets the data manager switch between Product and Unit of Measure
without displaying or replacing the other model's fields. The route holds the
read credential in session state, counts every complete plan, runs one capture
job, and publishes all frozen datasets through `OdooSourceCaptureService`.

`OdooCapturePublicationService` creates one tagged Parquet snapshot and one
protected origin sidecar per model. `OdooProvenanceRepository` advances the
complete manifest set, source selection, and snapshot pointers in one DuckDB
transaction. `WorkspaceDataVersionSourceService` accepts the same complete set
as the Data version's source evidence. A failed capture or publication leaves
the previous complete set current.

`derived_entities.py` routes optional lookup extraction, multi-column hierarchy,
and parent/child split rules through `DerivedEntityWorkspaceService`. These
rules remain plans until full preparation expands them over the frozen source.
For a one-field lookup, `preview_lookup` validates and materializes the current
immutable Parquet snapshot, then passes every chosen-field value to
`review_derived_entities`. The result reports exact source-row, output-row,
repeat, blank, incomplete-path, parent-row, and combined-spelling counts before
the rule can be saved. The review runs in a worker thread, reads no live Odoo
business records, publishes no artifact, and changes no source evidence.
The choice controls open forms within Stage 1 without adding browser-history
entries. A preview response reveals its form, while a saved rule redirects to
its saved card. Lookup and hierarchy choices that need Odoo record types open
the shared Odoo access page when the destination is not configured. A bounded
return token survives the connection check and local-stack actions. After the
connection is saved, `target.py` refreshes the record-type list and redirects
to the requesting Stage 1 form. The access check does not save a separation
rule or choose Stage 2 record types. A changed target still invalidates
dependent evidence through the existing target setup rules.

`HierarchicalLookupRule` is model-neutral. It binds two to five ordered stable
column keys from one accepted dataset, an arbitrary captured target model and
name field, and explicit missing-parent, missing-leaf, and all-blank policies.
`evaluate_hierarchy_path` is the shared preview and staging oracle. The rule
does not assume Product Categories or technical fields such as `parent_id` and
`categ_id`; Match data discovers compatible self-parent and consumer many2one
fields from the captured schema. The original one-column `DerivedEntityRule`
retains its version-4 meaning.

## Contract invariants

File intake accepts only bounded CSV and XLSX content. Inspection records the
file format, structure, formulas and errors, type suggestions, and bounded
samples. It inventories formulas but never executes them. Digit strings with
leading zeros remain text. Blank or duplicate headers block confirmation,
while other warnings require explicit acknowledgement.

Freeze assigns stable dataset and column identities and publishes every chosen
table as an immutable tagged Parquet snapshot with source-row lineage. The
selection and all snapshot pointers advance in one transaction. Mapping preview
and preparation read the verified snapshot rather than reopening CSV or XLSX.

An integrated Recipe run reaches the same freeze boundary from its run-owned
**Fresh data** page. It uses the selected Recipe revisions to propose the
physical tables and dataset names, but it still calls the ordinary confirmation,
freeze, and DataVersion projection services. Ordinary Authoring retains its
detailed table review; there is no second validation or snapshot implementation.

Each Odoo capture selection is append-only and bound to the current target
identity. Saving one model's selection does not contact Odoo and does not
replace another model's current selection. The complete set must contain one
plan for every model in the current schema, with distinct dataset names and at
most ten datasets. Current policy permits at most 50 closed scalar fields and
10,000 rows per model. The eligible value types include finite Odoo `float`
values, which the source snapshot records as numeric values and presents as
decimal candidates. The Odoo-source policy hash changes when this capture
surface changes, so an earlier saved selection cannot silently acquire the
new field type. This read-only capture change does not enable float updates to
the captured source instance. The browser also accepts one exact-match root
predicate on an eligible direct scalar field. `SourceWorkspaceService` validates
the field and value against the live schema before storing the value through
`ProtectedOdooCaptureFilterStore`. The encrypted artifact is bound to the
selection ID, version, DataVersion, and project. The version-5 selection JSON
contains only the ciphertext hash; it contains neither the predicate value nor
an Odoo record ID. `OdooSourceCaptureService` requires and verifies the artifact
before it builds a governed request. Missing or changed artifacts block capture.
The DuckDB admission check validates the selection's schema reference without
using its protected value; it discards that temporary validation request and
cannot use it for a live read.
Earlier version-4 selections remain readable. Editing a filtered plan reuses
the protected value and creates a new encrypted artifact for the new selection
version. The operator must explicitly remove it or enter a replacement to
change the record scope.
Version-6 selections mark a supporting model as `LINKED_ONLY`. They contain no
source record IDs and cannot carry a root filter. `OdooSourceCaptureService`
builds a protected dependency closure from the selected root records. It scans
only IDs, write dates, and eligible relationship fields before any business
values. It follows many-to-one and many-to-many links and uses one-to-many
fields to discover children whose inverse many-to-one field owns the portable
link. The closure deduplicates cycles and enforces four steps, 50,000 total
rows, 250,000 links, and the per-model row limit. It rejects missing or
inaccessible linked records and references outside a selected root set.
Membership is kept in memory inside the protected capture path. Linked value
reads use chunks of at most 100 IDs, and an empty linked set creates an empty
dataset without an unbounded model read. Capture compares IDs, write dates,
and stored links against discovery, then rescans the closure before publication
to detect changed membership or discovery links. The complete manifest set is
still promoted together. Because JSON-2 does not provide a database-wide
snapshot, a source change that reverts between checks remains a limitation.
The reader fetches 10, 100, or 500-row keyset
pages as the saved plan specifies. It shares the start and end identity and schema
checks across the set. Root models use one bounded value stream; linked models
use one stream for each protected 100-ID chunk. The live
reader accepts only service-generated requests. It exposes no raw domain,
arbitrary context, generic method, or caller-selected field path.

The source selection page passes metadata-safe many-to-one, many-to-many, and
one-to-many relationships outside the selected schema through
`propose_related_odoo_data`. The discovery engine first classifies non-writable
related, computed, and read-only links as Odoo managed. It then asks the ordered
versioned profiles for a recommendation. The standard profile recommends
common supporting data, optional business data, destination configuration,
separate processes, and excluded history. Product Category and Unit of Measure
links from `product.template`, plus the category link from `uom.uom`, are its
first qualified supporting recommendations. An unknown custom link has no
profile classification and appears under **Safe default for an unclassified
link** with the bounded `PRESERVE_LINKED` recommendation when its related model
exists in the current live catalogue. The data manager still saves that visible
choice. The web presenter owns the data-manager wording and keeps technical
field paths under **Support details**.

The source page keeps related-model selection beside this explanation. It
groups repeated relationship fields under one related model and presents one
recommended business outcome. Alternatives remain under **Advanced**. The
operator selects **Save related-data decisions** to persist the displayed
outcomes. `relationship_scope_decisions` expands each grouped choice into one
`OdooRelationshipScopeDecision` per source field. The decision stores the
source and related model, field, requiredness, handling, source-capture action,
and recommendation-profile provenance. **Keep linked value** is the ordinary
default: it reuses an exact destination match or creates the minimum required
record and closes optional graph expansion. **Transfer related records as migration data**
keeps the related model on the discovery frontier. **Use existing destination
records only** includes reached identity evidence, removes the model's outgoing
edges from the frontier, and later fixes its destination handling to
`reference_only`. **Do not include** omits the optional edge.

The leaf rule is capture-role sensitive. A match-existing linked-only model
contributes identity evidence and retains only a relationship that scopes that
identity. A preserve-linked model also remains a leaf, but retains a required
relationship needed for minimum creation and any relationship-scoped identity
component while suppressing other optional relationships. When the operator
explicitly changes the same model to a root plan,
`plan_odoo_source_capture` may traverse its reviewed outgoing or inverse child
relationships. A match-existing model still compiles to `reference_only` for
the destination.

Contract version 3 of `OdooRelationshipScope` adds `PRESERVE_LINKED` while
retaining the explicit root set introduced by version 2. The related-data route
carries roots across iterative revisions, so a model does not lose independent
root membership merely because another reviewed edge reaches it. Versions 1
and 2 remain readable; a changed review writes current version 3 evidence.

After each save, authenticated schema refresh loads the newly included record
types and `review_relationship_scope` compares every current schema edge with
the saved field-level decisions. Newly discovered edges and edges whose
relation, requiredness, handling, or profile provenance changed are shown as
**Needs review**. Saved inclusions and exclusions remain reviewed. Automatic
Odoo-managed and excluded-history outcomes need no extra click.
Separate-process classifications are editable recommendations, not capability
restrictions. The page repeats this review for each newly reached graph level and
shows **Related-data review complete** only when no selectable edge remains
pending. `OdooSourceCaptureService` applies the same completeness check before
assessment or capture, so bypassing the browser cannot freeze an incomplete
relationship graph. Missing related models are named blockers rather than
silent exclusions.

The revision-checked
`POST /workspaces/{workspace_id}/sources/odoo-related-data` validates every
submitted model against the current schema-derived policy and model catalogue.
It preserves unrelated model choices, replaces the selectable related subset,
and saves the model scope and field-level relationship scope in one DuckDB
transaction. `OdooRelationshipScope` has immutable revision rows and one
current pointer. The workspace schema version 16 upgrade adds those tables
without inventing decisions for older workspaces. A changed model scope
invalidates the prior schema before authenticated field evidence is refreshed.
A relationship-only change keeps current schema evidence but invalidates
capture plans, manifests, key governance, mapping, and later evidence through
`WorkspaceStateService.update_schema_scope`.

Once that review is complete,
`propose_guided_odoo_capture_plans` derives persistence-neutral defaults from
the saved root set and edge actions. Explicit roots and full-transfer children
receive every eligible scalar value field. Existing-only leaves receive their
reviewed scalar identity. Create-capable leaves receive that identity plus
required writable scalar inputs and qualified create-hook inputs. Relationship
components stay in protected origin evidence and are bound to portable related
identities during destination matching; they are not exposed as source Odoo
IDs or duplicated as ordinary value columns. The route persists every safe
missing plan, retains any plan already edited by the data manager, and opens a
named exception only when the model has no unambiguous identity, exceeds a
capture bound, or lacks eligible evidence. Automatic root plans deliberately
remain visible as all-matching plans so the data manager can add an exact root
filter before assessment.

The capture projection includes only reviewed relationship origins between
selected models. A linked-only match-existing model contributes its reached
records, scalar identity, and any relationship-scoped identity component, so
unrelated destination setup cannot enter the capture graph through it. A
linked-only preserve model projects only required outgoing relationships and
relationship-scoped identity components. An explicit root
may traverse its reviewed outgoing or inverse child relationships even when
its destination handling remains `reference_only`. A separate relationship
review lists eligible links between selected models before linked capture
assessment. The operator confirms that list as a group. This capture
confirmation remains distinct from the persisted field-level source-scope
decisions: the former confirms the exact fields that the pending capture will
traverse, while the latter records why each related record type was included
or omitted.

[ADR-016](../../decisions/README.md#adr-016--odoo-relationship-scope-combines-generic-decisions-with-profiles)
requires this Odoo-source discovery to extend the existing canonical
relationship engine. Stage 2 now persists four generic per-edge actions,
performs iterative explicit graph expansion, and gates assessment and freeze on
a complete review. The guided UI makes minimum preservation the normal path
without turning a profile recommendation into a capability rule.
Stage 4 completes each saved edge with its reviewed business key and
destination policy, then derives the existing `RelationshipMapping` and
`RelationshipResolver` values. Stage 5 uses the shared dependency extractor
over that projection. Do not describe further integration work as a
requirement to add another model-specific workflow rule or another
relationship engine.

Each identity check computes one small company-scope fingerprint from the
primary and available company IDs. Assessment performs one identity and schema
check for the complete set. Capture performs one pair before and after the
complete set. Business-value pages are neither rescanned nor hashed, and consistency
validation does not compute a digest unless the workflow needs an evidence or
form token.

## Code references

| Role | Code |
| --- | --- |
| File and selection orchestration | [`SourceWorkspaceService`](../../../src/impodo/application/source_workspace_service.py) |
| Odoo relationship recommendations | [`odoo_relationship_profiles.py`](../../../src/impodo/domain/odoo_relationship_profiles.py) |
| Odoo relationship scope discovery | [`odoo_source_scope.py`](../../../src/impodo/domain/odoo_source_scope.py) |
| Versioned Odoo relationship decisions | [`odoo_relationship_scope.py`](../../../src/impodo/domain/odoo_relationship_scope.py) |
| Canonical Odoo relationship projection | [`odoo_relationship_compilation.py`](../../../src/impodo/domain/workspace/odoo_relationship_compilation.py) |
| Current relationship-scope persistence | [`WorkspaceStateRepository`](../../../src/impodo/adapters/duckdb/workspace_state_repository.py) |
| Isolated source workers | [`source_worker.py`](../../../src/impodo/application/data_version/source_worker.py) |
| Shared source-file browser commands | [`source_file_commands.py`](../../../src/impodo/web/source_file_commands.py) |
| Odoo source capture | [`OdooSourceCaptureService`](../../../src/impodo/application/odoo_source_capture_service.py) |
| Related Odoo scope policy | [`odoo_source_scope.py`](../../../src/impodo/domain/odoo_source_scope.py) |
| Related Odoo scope presenter | [`odoo_source_scope.py`](../../../src/impodo/web/presenters/odoo_source_scope.py) |
| Protected linked-record closure | [`discover_dependency_closure`](../../../src/impodo/application/odoo_dependency_capture.py) |
| Protected root predicates | [`ProtectedOdooCaptureFilterStore`](../../../src/impodo/adapters/protected_odoo_capture_filters.py) |
| Atomic Odoo capture-set publication | [`OdooCapturePublicationService`](../../../src/impodo/application/odoo_capture_publication_service.py) |
| Protected Odoo origin evidence | [`OdooProvenanceService`](../../../src/impodo/application/odoo_provenance_service.py) |
| Data-version source acceptance | [`WorkspaceDataVersionSourceService`](../../../src/impodo/application/workspace_data_version_source_service.py) |
| Derived downstream source gate | [`assess_source_stage_readiness`](../../../src/impodo/application/workspace/source_readiness.py) |
| Odoo capture jobs | [`OdooCaptureJobManager`](../../../src/impodo/application/odoo_capture_job_service.py) |
| Related-dataset plans | [`DerivedEntityWorkspaceService`](../../../src/impodo/application/workspace/derived_entities.py) |
| Complete one-field result review | [`review_derived_entities`](../../../src/impodo/domain/workspace/derived_entities.py) |
| Shared one-field grouping semantics | [`_group_lookup_values`](../../../src/impodo/domain/workspace/derived_entities.py) |
| Multi-column hierarchy contract and path oracle | [`HierarchicalLookupRule`](../../../src/impodo/domain/workspace/derived_entities.py) |
| Authenticated hierarchy-tutorial screenshot capture | [`capture_hierarchy_tutorial_screenshots.py`](../../../scripts/capture_hierarchy_tutorial_screenshots.py) |
| Source routes | [`sources.py`](../../../src/impodo/web/routers/sources.py) |
| Related-dataset routes | [`derived_entities.py`](../../../src/impodo/web/routers/derived_entities.py) |

## Evidence and state

The draft DataVersion package stores file references, content hashes, bounded
catalogues, and chosen physical-table configuration. The frozen
`SourceSelection` binds stable
dataset IDs, physical schema, row counts, source evidence hashes, and Parquet
storage. Odoo capture adds one selection, provenance sidecar, and target
binding per dataset without using numeric Odoo IDs as portable business
values. The model-keyed selection pointers and dataset-keyed manifest pointers
identify the exact current set. The `SourceSelection` binds all datasets as
one atomic source version.

Related-dataset rules are versioned workspace-owned evidence and must retain
complete source lineage when materialized later.

## Completion and navigation

File mode completes when a source selection exists and then unlocks Odoo data.
Odoo mode deliberately reverses the first two responsibilities. **Select data
to download** captures eligible fields and model-specific plans, then
**Download and freeze** publishes the selection. It then unlocks the separately
bound cross-instance destination workflow. Stage 4 connects that destination
and matches every selected model. Stage 5 derives the generic relationship
order and approves an exact aggregate transfer package. Stage 6 rechecks that
package through a fresh read-only destination call, then performs one more
no-write destination check, stages an exact execution snapshot, requires a
separate hash-bound confirmation, then journals, loads, and reads back the
approved destination changes.

`assess_source_stage_readiness` derives this gate from the current workspace
mode, schema status, schema model set, capture-plan model set, and frozen source
selection. It is not a stored completion flag. For Odoo sources, every schema
model must have exactly one current capture plan and a complete frozen source
selection must exist. A stale direct URL returns to `schema` or `sources` with
the unfinished action. `enqueue_preparation` repeats the gate before it creates
or retries a job, while `PreparationService` retains its evidence validation as
defence in depth.

The source capture and destination checks use two distinct credential roles.
The source-fetch key cannot satisfy destination matching. The one destination
transfer key supports destination matching, read-only preflight, and no-write
load preparation. Only the explicit Stage 6 load confirmation re-probes that same key
for the exact write and read-back scope. No third credential role is used.

## Invalidation and recovery

Before file-table freeze, add/remove commands use workbench revision checks and
delete only the selected DataVersion file and its dependent draft metadata.
After freeze,
source mutation fails closed. A changed hash, selection, capture, or
related-dataset plan invalidates downstream evidence; regenerate rather than
editing stored artifacts. Changing Odoo model scope or one capture plan locks
later browser stages until every current plan is complete and the operator
explicitly freezes the complete set again. The application never promotes a
partial set or automatically freezes it on the operator's behalf.

Background Odoo capture exposes explicit cancel and status routes. Do not
interpret an interrupted job as a published snapshot.

## Odoo compatibility and performance

Odoo source capture must remain bounded by an explicit selection and eligible
field policy. Page reads are batched; adding per-row metadata or relationship
lookups would create an N+1 regression. Preparation must consume the frozen
snapshot and make zero Odoo calls.

Source review, saved tables, related-table cards, and the adjacent file and Odoo
access setup pages render in bounded `run_page_read` workers. The access
middleware also resolves authorization and applies its local route policy in
one worker. Each scope releases its database owners before returning to the
event loop.

[`BrowserQueryService.get_source_page`](../../../src/impodo/application/browser_queries.py)
passes the source package already verified
for workspace state into `SourceReviewPage`. File review and saved related-rule
cards reuse its catalogues, configurations, and selection. Those page reads add
no database work as the saved rule count grows. The explicit **Review resulting
table** command is different: it opens exactly one verified local source
snapshot and scans the selected field outside the event loop. It accepts at
most 50,000 source rows and 5,000 resulting related records. It performs zero
Odoo calls. The package reader checks hashes inside one read transaction; there
is no transaction shared across stores.

`review_derived_entities` and the full preparation evaluator both use
`_group_lookup_values`. Capitalization, Unicode normalization, spacing,
optional parent paths, repeated values, blanks, and incomplete paths therefore
have the same meaning in the browser review and in later preparation. The
review is recalculated from accepted evidence when requested; it is not stored
as a second source artifact.

The Odoo capture page reviews saved plans once when protected filters are
present. It verifies each protected filter once and keeps errors keyed by model.
Capture commands continue to read and validate the current complete selection
set independently. Store validators use
[`read_table_columns`](../../../src/impodo/adapters/duckdb/schema/structure.py)
to inspect ordered columns in one catalogue query per check and continue to
reject incompatible structures on fresh reads.

The Odoo capture presenter shares its safe read-credential status with the
read-key dialog when both refer to the same canonical owner and target.
The next request reads status again, and capture commands retrieve and validate
current credentials independently. Capture history remains complete; the page
still offers every saved capture rather than hiding older evidence.

The current derived or materialized preparation path has a lower row limit
than exact direct mappings; keep that limit visible rather than silently
falling back to unbounded Python work.

## Verification

`capture_hierarchy_tutorial_screenshots.py::capture` creates an isolated
fictional Product workspace and captures the current authenticated Stage 1
one-field result review, hierarchy form, and hierarchy preview in Edge. It
exercises complete, missing-parent, missing-leaf, and all-blank paths without
reading or changing an operator workspace.

- [`tests/integration/duckdb/test_workspace.py`](../../../tests/integration/duckdb/test_workspace.py)
- [`tests/application/data_version/test_source_worker.py`](../../../tests/application/data_version/test_source_worker.py)
- [`tests/domain/data_version/test_source_snapshot.py`](../../../tests/domain/data_version/test_source_snapshot.py)
- [`tests/integration/odoo/test_source_capture.py`](../../../tests/integration/odoo/test_source_capture.py)
- [`tests/application/data_version/test_odoo_capture_publication.py`](../../../tests/application/data_version/test_odoo_capture_publication.py)
- [`tests/application/data_version/test_odoo_capture_jobs.py`](../../../tests/application/data_version/test_odoo_capture_jobs.py)
- [`tests/domain/test_odoo_source_scope.py`](../../../tests/domain/test_odoo_source_scope.py)
- [`tests/domain/test_odoo_relationship_scope.py`](../../../tests/domain/test_odoo_relationship_scope.py)
- [`src/impodo/domain/guided_odoo_capture.py`](../../../src/impodo/domain/guided_odoo_capture.py)
- [`tests/domain/test_guided_odoo_capture.py`](../../../tests/domain/test_guided_odoo_capture.py)
- [`tests/integration/web/test_odoo_source_scope_presenter.py`](../../../tests/integration/web/test_odoo_source_scope_presenter.py)
- [`tests/architecture/test_workspace_schema_contract.py`](../../../tests/architecture/test_workspace_schema_contract.py)
- [`tests/integration/duckdb/test_forward_upgrades.py`](../../../tests/integration/duckdb/test_forward_upgrades.py)
- [`tests/application/workspace/test_derived_entities.py`](../../../tests/application/workspace/test_derived_entities.py)
- [`tests/integration/web/test_source_workflow.py`](../../../tests/integration/web/test_source_workflow.py)
- [`tests/integration/web/test_stage12_page_loading.py`](../../../tests/integration/web/test_stage12_page_loading.py)
- [`tests/integration/duckdb/test_schema_structure.py`](../../../tests/integration/duckdb/test_schema_structure.py)
- [`tests/e2e/test_derived_entities_navigation.py`](../../../tests/e2e/test_derived_entities_navigation.py)
- [`tests/integration/web/test_target_workflow.py`](../../../tests/integration/web/test_target_workflow.py)

Cover file hashing, configuration, pre-freeze replacement, post-freeze refusal,
per-model Odoo plans, complete-set capture, atomic publication, capture bounds,
cancellation, lineage, and both navigation variants.

## Related documentation

- [ADR-016: Generic Odoo relationship decisions and profiles](../../decisions/README.md#adr-016--odoo-relationship-scope-combines-generic-decisions-with-profiles)
- [Stage 1 and 2 page-loading optimization proposal](../../plans/stage-1-and-2-page-loading.md)
- [Page-loading optimization evidence](../../testing/stage12-page-loading-2026-10-07.md)
- [Shared page-read measurements](../../testing/stage12-shared-page-reads-2026-10-07.md)
- [Shared page-read audit helper](../../testing/evidence/stage12-shared-page-reads-2026-10-07/audit.py)
- [User guide: Source data](../../user/workflow/01-source-data.md)
- [Project lifecycle contract](../contracts/project-lifecycle.md)
- [Workflow evidence lifecycle](../contracts/evidence-lifecycle.md)
- [Related-table authoring](../../user/guides/related-tables.md)
- [Related-table authoring guide](../../user/guides/related-tables.md)
