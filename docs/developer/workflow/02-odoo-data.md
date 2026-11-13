---
audience: developer
stage: odoo
status: current
---

# Odoo data

## Responsibility

Odoo data captures selected qualified Odoo 19 or final Odoo 20 model and field
metadata. The shared
**Odoo access** page configures the destination and checking credential outside
the numbered file-source stages. In file mode, Odoo data also governs the
business keys used by mapping and comparison. It owns
the confirmed target-schema boundary. It keeps source-read checks separate from
destination-read checks and records where the captured metadata came from.

It does not read an unrestricted business-record export and does not expose a
generic RPC escape hatch.

## Contextual user guidance

The record-scope and matching-rule headings link to their own user-guide
sections through `odoo-scope` and `odoo-identity`. The shared
[guide-help contract](00-project-setup.md#data-manager-concept-and-guide-help)
owns these static links and their coverage checks.

## Entry conditions

File mode requires frozen source data before Stage 2 metadata capture. Shared
Odoo access can be configured during Stage 1. If the destination is not
configured when the schema route opens, it redirects to Odoo access and returns
after a successful read-only check. Odoo source mode already has its source connection
and reaches schema capture before source records are frozen because eligible
fields define what may be captured.

The file-source sidebar links directly to **Odoo access** outside the six
numbered stages. Stage 2 lists only **Choose Odoo records**. A Stage 1 lookup
or hierarchy request carries one of two allowlisted return tokens through
connection testing and local-stack setup. Saving a checked connection then
refreshes the model list and returns to the requesting form. A missing key on
an already configured Remote target uses the inline read-key dialog and
resumes the same Stage 1 model refresh.

The schema and Odoo capture pages render the existing role-bound credential
status through `_saved_read_credential.html`. The status names the source or
checking role and reports its actual session or vault retention without
rendering the secret. Empty replacement fields preserve the existing key.
Destination transfer credentials retain their separate target and role
binding. Retention controls reflect the saved storage class on return.
The [credential visibility tests](../../../tests/integration/web/test_credential_retention_visibility.py)
exercise save, return, and blank replacement for both supported majors.

JSON-2 read failures record only the bounded model, allowlisted method, HTTP
status, category, and actual attempt count. Authentication failures retain
their classification; a permanent 404 is not retried. URLs, credentials,
request values, and server response bodies remain excluded. A 404 identifies
an unavailable operation without proving which server-side condition caused
it.

## Implementation flow

### Matching-rule scope selection

The **Within (optional)** control renders labelled checkboxes from captured
field metadata. Operators can select several scope fields without entering
technical names. `schema-scope-fields.js` keeps the selected labels and ordered
technical mirror current. It loads before `schema.js`, which owns the draft,
conflict and confirmation states. Existing rules retain their scope order.
The `schema/_scope_picker.html` partial groups selected labels, removal actions
and a searchable disclosure. Scope names are mirrored in a hidden form input
instead of a second visible editor. Search never changes selected fields, and
Enter in the search box cannot submit the governance form. Escape closes the
disclosure and returns focus to its summary.

The governance route reads repeated `scope_choice_*` form values when the
checkbox-presence marker is submitted. Checked fields are authoritative, so an
explicit empty selection clears a stale mirror even without JavaScript.
Historical forms still use the existing scalar or combined-field contract.
The same domain validation rejects duplicate or incompatible fields. The
picker reads captured metadata only and adds no Odoo or DuckDB requests.

The [scope-selection regressions](../../../tests/integration/web/test_stage2_scope_selection.py)
cover saving, reopening, error recovery and clearing for Odoo 19, Odoo 20 and
a custom model. The [interaction tests](../../../tests/e2e/schema-scope-fields.test.cjs)
cover labelled selection, field order, search, keyboard behaviour and removal
of individual or all scopes.

### Company selection and protected ownership

The operator confirms the load company in
[Match data](03-match-data.md#company-and-business-expectations), after the
connection and field discovery. File-target discovery skips `default_get`
until a company has been confirmed. JSON-2 metadata reads use the optional
`include_create_defaults` flag; the fixed local-shell script applies the
same condition. Source capture retains its independently pinned context.
Stage 3 refreshes company-specific defaults through `check_refresh`; this
does not change field metadata hashes merely to record a company choice.

`odoo_company.py` exposes closed POST commands to read available companies
and confirm one operation company. The commands require the current session,
CSRF token and Project-edit authority. Confirmation rechecks the workspace
revision, credential generation and available choice after the remote read.
Available choices come from the authenticated user's active companies through
bounded JSON-2 reads, or from the fixed local-shell reader. Neither path
changes the user's default company.

`MigrationRunTargetSetup` owns the company binding hash and its workspace
owner. The initial workspace-engine schema creates the immutable
`odoo_company_binding` companion table through
[`create_odoo_company_bindings`](../../../src/impodo/adapters/duckdb/schema/odoo_company.py).
The initial registry schema creates the pointer columns. The protected DTO
verifies its exact target and content hash when projected into `WorkspaceState`.
A new project requires an explicit company confirmation. No numeric company ID
enters a Recipe or registry business payload.

Exact schema validation rejects unknown tables, columns, and mixed layouts.
The [company workflow tests](../../../tests/integration/web/test_odoo_company.py)
cover saved choices, target binding, and invalidation. The
[storage tests](../../../tests/integration/duckdb/test_forward_upgrades.py)
cover fresh initialization and rejection of unsupported generations.

Materialization copies the protected immutable company DTO into each isolated
application workspace. Its current run binding hash remains authoritative.
The browser and preparation worker read the local copy and verify that hash;
neither opens a sibling setup workspace during a workspace-bound command.
The Recipe-run browser check requires this company's confirmation before it
can activate application workspaces.

The context contains only `allowed_company_ids` with the single confirmed
company. Target schema, defaults, supporting choices, comparison, writer and
read-back factories use that context. Local shell reads apply it to their
environment; their context evidence does not claim an authenticated API
principal or model permission probe. Source capture retains its independently
pinned context. Odoo-to-Odoo destination selection uses the destination key
and projects `destination_company` only into the destination workspace.

A safe change retires supporting lookups and preparation, comparison and
approval pointers while preserving source, schema and mapping rules. A
company-only schema refresh preserves mapping meaning only when the target,
principal, permissions and field structure agree. A freshly verified key
generation can replace the old generation during that company refresh.
Changed defaults
and company context are refreshed through a narrow guarded repository write.
Other changes retain the pending-schema-review path. Active, partially applied
or uncertain writes block a company change. An activated non-authoring run
requires a fresh run to choose a different company.

Local command preparation and publication use separate bounded synchronous
operations; every DuckDB owner closes before the Odoo read. The company-save
fixture measures ten physical connections across four owner paths, two schema
checks and zero retries. It performs no source-row or Parquet scan. This is
controlled structural evidence, not a live latency benchmark.

`test_odoo_company.py` covers browser persistence, stale and unavailable
choices, transfer isolation, local context and active-load rejection.
`test_company_transport.py` covers both qualified majors, a model without a
company field, shared records, load context, read-back and create-access
rejection. The joined Recipe tests rebind another company on another database;
the 65,299-row test retains two combined outputs and confirms a fresh company
for Recipe reuse. Hosted Linux deployment and live Odoo writes are separate
qualification activities; these fictional tests do not certify them.

The implementation and focused checks are:

- [Company-domain interpreter](../../../src/impodo/domain/odoo/company_domains.py)
  and its [closed-expression tests](../../../tests/domain/odoo/test_company_domains.py).
- [Grouped Odoo compatibility reads](../../../src/impodo/adapters/odoo/company_checks.py)
  and [protected preflight application](../../../src/impodo/domain/preparation/company_references.py),
  covered by [shared and parent-company tests](../../../tests/integration/odoo/test_company_reference_checks.py).
- [Native transport tests](../../../tests/integration/odoo/test_company_transport.py)
  for model-independent matching, loading, read-back and create access.
- [Joined Recipe reuse](../../../tests/integration/web/test_joined_recipe_preparation.py)
  and [65,299-row preparation with two combined outputs](../../../tests/integration/web/test_mixed_join_preparation.py).

`target.py` and `OdooConnectionTestService` identify the exact database and
verify either source-read or destination-read purpose without discovering
models or fields. `schema.py` then refreshes the permitted model catalogue,
saves the selected scope, captures field details, and, for file mode, submits
key governance. `SchemaWorkspaceService` coordinates these operations through
separate ports for the model catalogue, schema catalogue, source selection,
and mapping invalidation.

For Odoo sources, the main-record picker submits only relationship-scope roots.
When those roots are unchanged, `prepare_schema_scope` passes the existing
complete model set to `update_schema_scope`, retaining automatically linked
records and current capture evidence. The same authorization, revision, and
workspace-state checks still run. A genuine main-record change invalidates
dependent work and captures the new field details through the existing path.
The [main-record return regressions](../../../tests/integration/web/test_odoo_main_scope_return.py)
cover both supported Odoo majors and local and remote metadata, including
unchanged choices, changed choices, stale revisions, and bounded database work.

After a live destination capture, `supporting_models.py` derives direct
relationships from the selected schema. It classifies each relationship as
reuse existing, checked default, Odoo managed, or review incoming. The Stage 2
presenter joins that domain result with the saved model catalogue and the
versioned relationship profiles, then projects a preview of the later generic
outcomes. Ordinary links show **Keep linked value**; destination configuration
shows **Use existing destination records only**; Odoo-managed links remain
separate. This preview cannot promote a related model into top-level transfer
scope. The detailed edge decision remains in the source relationship review.
Opening the page remains local: this projection is not persisted and makes no
Odoo request.

### Source-aware supporting proposal query

`SchemaWorkspaceService.supporting_data_plan` now provides a separate,
read-only application query for file-source dependency proposals. It authorizes
the supplied Actor before reading the current workspace, accepted source
selection, captured schema, and saved source-preparation plan. The caller must
supply the expected source, schema, and preparation hashes. Changed inputs,
another workspace's preparation, or a changed target reject the query.

`supporting_sources.py` describes physical tables and saved lookup, hierarchy,
split, and structural outputs from metadata. It reuses the existing generated
dataset identities and map-eligibility rules. It does not evaluate source rows,
build previews, load source artifacts, or obtain a mapping-source selection.
Saved lookup rules provide their explicit target-model hint. Physical and
structural tables require an explicit dataset/model intent; similar labels do
not establish a binding. Query intents are proposals bound to these exact
inputs, not persisted scope decisions.

`supporting_plan.py` indexes those descriptors and captured models once. It
retains each required or explicitly intended relationship edge. Optional
collections do not become blocking dependencies merely because the schema
declares an inverse field. The direct Stage 2 preview can still show them.
`close_supporting_plan` derives optional intent from actual relationship,
identity, and scope providers. It retains an inverse child only when its
incoming mapping points to the exact parent dataset. Explicitly skipped
optional fields remain unused; a conflicting actual provider is still rejected.
Reviewed reuse, checked-default, and Odoo-managed choices take effect before
candidate expansion. A target-catalog mapping also retains reuse intent when
no supporting choice was saved. These rules prevent unrelated accepted tables
from creating spurious ambiguity or missing-metadata errors.
Relationship intents retain `source_dataset_id`. Two tables mapped to the same
Odoo model can therefore use independently selected parent providers. Conflicts
within one table, unknown owners, and unavailable incoming tables remain
blocking. An inverse child collection can include several incoming tables
whose parent mappings point to the same table, including that table's own
hierarchy rows. These providers contribute children; they do not compete to
supply a single parent. `MAPPING_DEFINED` records multiple fully specified table-owned
providers on one model-field edge; it does not authorize writes or override a
reviewed supporting choice. The
[generic hierarchy regressions](../../../tests/domain/workspace/test_hierarchical_supporting_plan.py)
cover locations, categories, account groups, and a custom model. The
[persisted-review regressions](../../../tests/integration/web/test_hierarchical_supporting_review.py)
exercise authoring and preparation for both majors in LOCAL and REMOTE modes.
The walk follows uniquely proposed incoming models, preserves repeated uses of
one related model as separate field edges, and deduplicates cycles. Ambiguous
tables, unavailable incoming metadata, invalid intent, and limit exhaustion
produce an explicitly incomplete result. A complete proposal does not prove
relationship values or authorize reads, writes, or a stage transition.

Contract version 3 binds reviewed choices, actual provider intent, and source
table ownership into the derived proposal hash. Saved reviews, schema hashes,
source evidence, and
mapping identities retain their existing contracts. The planner permits
100 reached models, 1,000 relationship edges, five
dependency expansions from the selected roots, 1,000 dataset descriptors, and
200 source-preparation rules. Existing checked defaults and Odoo-managed
fields retain their direct-classifier meaning. The browser keeps its direct
supporting preview and also presents the reviewed file-source decisions
described below. Odoo-source edge ownership is unchanged.

[`SupportingPlanIncompleteError`](../../../src/impodo/domain/workspace/supporting_errors.py)
retains metadata for failed edges and names the relationship in browser
guidance. The intent regressions are covered by the
[domain cases](../../../tests/domain/workspace/test_supporting_plan_intent.py),
[local and remote authoring and preparation checks](../../../tests/integration/web/test_supporting_mapping_intent.py),
and [Recipe run checks](../../../tests/integration/web/test_recipe_supporting_intent.py)
for Odoo 19 and Odoo 20.

The composition injects source, schema, preparation, workspace, and
authorization ports. There is no PostgreSQL or DuckDB dependency in the
application query or pure planner. A local caller owns one synchronous read
operation. A shared-server caller must supply authorized verified evidence
through its adapters; this query does not permit a Linux web request to open
worker-owned DuckDB state.

### Reviewed file-source supporting decisions

`SupportingReviewService` validates closed choices for each captured parent
field: incoming data, existing Odoo records, a checked default, Odoo ownership,
or an unused optional field. Incoming choices require a compatible accepted
descriptor and an already selected related business model. A save cannot widen
the write scope. Actual field providers and value-level resolution remain
owned by Match data.

The review, dependency proposal, and mapping-conflict check share the qualified
create-hook policy from `create_field_policy.py`. With the exact standard
`product.template.product_variant_ids` One2many to `product.product` on a
supported Odoo version, **Handled by Odoo** is a valid saved choice. Odoo
generates these variants when it creates the Product. This choice does not
add a variant table to the write scope. Missing version evidence, custom
collections, and incompatible field shapes retain their normal review rules.
The Product name remains an independently checked input to the create hook.

The [Product supporting-review browser tests](../../../tests/integration/web/test_product_supporting_review.py)
verify saving, reopening, matching-rule confirmation, and the downstream
mapping check on Odoo 19 and 20. The
[Product supporting-review capture helper](../../../scripts/capture_product_supporting_review.py)
reproduces the saved choice in an isolated fictional workspace.

`supporting_data_review` stores immutable complete revisions in the contained
workspace engine. The initial schema creates this table and
`supporting_review_projection`, a rebuildable local summary containing bounded
counts and verified evidence hashes. No review is approved implicitly.
An empty saved revision contains no supporting choices to renew.
`SupportingReviewService.validate_review` verifies its workspace and normalizes
an empty choice set against current metadata, then returns no effective review.
New required relationships still block. A stale empty revision cannot block
Match data solely because its input binding changed. No hash or saved revision
is rewritten, and nonempty reviews retain every freshness check. The
[supporting-review application tests](../../../tests/application/workspace/test_supporting_review.py)
cover both majors and access modes; the
[mapping browser regressions](../../../tests/integration/web/test_supporting_mapping_intent.py)
exercise Save followed by Check matches and verify committed receipts.

Publication compares the expected review version, reserves the operation UUID,
checks retry meaning and Actor identity, and writes the revision and audit in
one transaction. An exact retry returns the original revision. Changed choices
retire dependent mapping and preparation evidence while preserving accepted
source data and saved business-key meaning.

The binding hash covers the accepted source, preparation rules, captured schema,
selected business models, target and Odoo version, reference-policy hashes, and
captured credential, principal, permission and company-context hashes. Required
missing or stale choices block `SchemaWorkspaceService.govern_complete` and
appear through `WorkflowIssueProjection`. The page reads the saved review and
preparation plan once each, reusing its already loaded workspace, source,
schema, and governance evidence. Candidate table names appear in one shared
bounded list rather than a repeated catalogue per relationship.

Reuse choices retain an exact key, scope and display projection derived from
`GovernedReferenceRequest`, the reviewed reference policies, or the bounded
supporting-match probe. Caller-provided wider shapes are rejected. The explicit
`/schema/supporting/check` command prepares immutable metadata and grouped
requirements inside one local operation, releases every database owner, and
calls the existing read-only supporting capture path. It rechecks all current
inputs and the exact saved review inside a second local operation before
publishing any returned lookup. Changed access or input evidence rejects the
old result. The existing capture limits, returned-metadata verification, and
protected lookup snapshots remain authoritative.

On Odoo 19 and Odoo 20, `reference_keys.py` also allows exact reviewed
candidates with one unscoped `name` key through captured `many2many` relationships. Companies and Account
Tags use their reviewed `name` candidates, including
`account.account.company_ids` and `account.account.tag_ids`. The bounded
`MATCH_CHOICES` probe denies unreviewed list identities and wider projections.
These candidates do not establish globally unique standard reference keys.
Duplicate names stay ambiguous in the target-bound lookup. The shared policy
checks returned candidate field types before accepting them as captured
governed evidence. Account Tag names can repeat across applicability or country,
so a repeated name remains blocking rather than selecting the first tag.

Saving this review performs no Odoo read. The explicit check groups company
relationships with the same identity into one read and retains the existing
local operation boundaries before reading and before publishing. Recipe
publication uses the verified lookup's field contracts; subsequent runs check
their own supporting metadata and values. Reference policy version 7 changes the
current hashes for both majors. Historical hashes remain readable, while
currentness checks require renewed review and lookup evidence. Reference policy
version 8 adds company-context account-code candidates and exact account-name
choices for both majors. It does not treat account codes as globally unique or
introduce a singular company field on accounts. Current lookups must be
captured under the new policy before reuse.

The save and check routes authorize the request Actor before governed work.
They expose no generic Odoo method or write. The complete decisions, read
scopes, approvals and lookup values remain data-plane evidence. A future
server adapter must supply authorized worker evidence and verified receipt
projections; these routes grant no Linux web access to worker-owned DuckDB.

### Recipe closure and downstream freshness

`SupportingMappingGuard` reconciles reviewed choices with the bounded
source-aware plan before mapping validation or submission, Recipe compilation,
and preparation. Stale inputs and contradictory field providers block those
actions. Historical file mappings without a saved review retain their existing
path. Odoo-source relationship compilation continues to own full incoming
migration, minimum preservation, destination reuse, and ignored edges.

`RecipeCompiler` stores reviewed choices in the target contract using existing
logical dataset identities. It retains metadata for intentionally unused and
Odoo-managed relationships too. `RecipeApplicationService` rebinds incoming
identities and checks the materialized rules before publishing a mapping.
The additive choice data constrains existing mapping meaning; it adds no
resolver and contains no target-access evidence.

`application/recipe/odoo_requirements.py::extract_odoo_requirements` owns shared
portable extraction. The run query still authorizes and bulk-loads exact
revisions once, then verifies hashes and merges in memory. Its result retains
every revision, Recipe reason and relationship path while deduplicating
compatible reads. Conflicting choices stop the query without choosing a winner.

Permission hashes include the probed model scope. Supporting hashes therefore
remain separate from primary-schema permission hashes. The explicit review
check verifies current primary-scope access and exact supporting access. Equal
scopes share one identity probe; distinct scopes use two bounded probes.
Page rendering performs neither. Changed captured permission evidence also
retires a review through its input binding.

The review transaction publishes `supporting_review_projection` with the
decision and audit. It contains bounded counts and input, decision and complete
evidence hashes. Authorized `summary` and `rebuild_summary` service methods read
or reconstruct that metadata without Odoo capture. Currentness requires a
matching verified input binding; future PostgreSQL publication also requires a
verified worker receipt. Identical decision meaning preserves dependent mapping
and preparation pointers when another operation records a new revision.

The mapping guard reads each metadata owner once. Preparation reuses its loaded
workspace, source and rule metadata. The controlled guard test permits four
physical owners and zero retries. The page probe used six connections, one
schema check, zero retries and zero Odoo calls in both saved and unreviewed
states. These are fixture measurements, not live-server latency qualification.

### Capture and refresh

For a non-Production target, the connection form can explicitly keep the same
submitted or already stored secret for later loading. `target.py` writes two
target-bound vault envelopes with distinct `READ` and `WRITE` roles and audits
both credential generations. The option grants no write operation and performs
no speculative write call. Stage 6 probes the saved `WRITE` role against the
exact reviewed execution scope before constructing a writer. Production setup
does not expose this option and continues to require a separate limited write
key.

The first capture publishes the selected target schema. A later **Check for
Odoo changes** builds and validates a candidate before publication. The
service compares its semantic fingerprint with the current catalogue:

- An unchanged result keeps the current catalogue content hash and dependent
  current pointers. It records the check time, actor, and current access
  binding, and clears any older pending candidate.
- A changed result stores the candidate and its bounded model/field change
  summary beside the current catalogue. It marks the stage **Needs attention**
  and blocks new Odoo source freezes without invalidating current evidence.
- **Use updated Odoo details** compares the candidate and current hashes again,
  then atomically publishes the candidate and invalidates schema governance,
  source-capture selection and snapshot pointers, mapping, and later evidence.

The semantic fingerprint binds technical target identity, selected model
scope, field types and flags, relationship metadata, selection codes,
constraints, usable required create defaults, origin, and read-access
meaning. It excludes capture/check times, actors, credential generations, and
translated display labels. Those facts are freshness, access provenance, or
presentation rather than schema structure.

Local capture uses the isolated local reader. Remote capture uses the narrow
JSON-2 read connector. Both normalize metadata into the same domain catalogue
before governance is saved.

When an adapter supplies `allowed_company_ids`, the identity probe verifies
that each company is available to the authenticated user and preserves their
order. The first company determines Odoo's current company and therefore the
value of company-dependent fields. Empty, duplicated or invalid selections
cannot produce identity evidence. Without a supplied context, the existing
user-default and available-company behavior remains unchanged. The explicit
browser company selection supplies this adapter context.

## Saved Odoo data and repeat restoration

The project-level **Saved Odoo data** path preserves selected clean records
for later same-major restoration. It reuses an Odoo-source workspace's reviewed
model, field, filter, archived-record, and relationship choices. It does not
introduce a separate unrestricted model picker or make a Recipe own source rows.
The paired [user guide](../../user/guides/saved-odoo-data.md) explains the two
operator-facing levels and exact controls.

`SavedOdooDataDefinition` owns reusable capture meaning and a configuration
revision. `SavedOdooDataVersion` references a complete frozen DataVersion and
its manifest hashes and model row counts. Publishing data advances the saved
definition's optimistic revision without silently revising its configuration.
Capture configuration contract version 2 binds the reviewed relationship scope
alongside the selected models and fields.
`SavedOdooRestoreProfile` separately remembers destination settings, matching
keys, field rules, and required-value providers. It contains no API key.

| Browser command | Owner and result |
| --- | --- |
| `GET /projects/{project_id}/saved-odoo-data` | Projects visible definitions, saved versions, destination profiles, and current action status. It does not capture or restore data. |
| `POST /projects/{project_id}/saved-odoo-data/configure` | Rechecks the source company and metadata, then creates or revises the capture definition. It does not download business rows. |
| `POST /projects/{project_id}/saved-odoo-data/{saved_data_id}/update` | Enqueues a complete refresh. The previous complete saved version remains usable if the new attempt fails. |
| `POST /projects/{project_id}/saved-odoo-data/{saved_data_id}/restore` | Checks the destination under the selected version and policies. `Check destination` is read-only; explicitly confirmed `Restore all` can continue through checked execution and read-back. |
| `POST /projects/{project_id}/saved-odoo-data/{saved_data_id}/versions/{version_number}/download` | Exports that exact version and configuration inside a password-protected `.impodo` file. |
| `POST /projects/{project_id}/saved-odoo-data/import` | Authenticates and validates a complete package before admitting new source ownership in the selected project. |

### Source context and complete refresh

Source company ownership is separate from destination company selection.
Workspace schema version 7 adds `odoo_saved_source_company`; its serialized
`OdooCompanySelection` is checked against its binding hash. Selecting a changed
source company retires dependent metadata and capture plans. A frozen source
requires a fresh successor before changing that context. The run's destination
company binding remains separate.

`SavedOdooDataService.configure` and `refresh` separate local admission from
remote source checks. `All restorable fields` reevaluates eligible scalar
fields against current metadata. Explicit selection keeps its reviewed field
list. Both paths preserve the reviewed relationship scope. The source capability
policy still excludes company-dependent property values, translations, binary
content, and other ineligible fields; the label does not broaden that policy.

`OdooSavedDataWorkspaceService.prepare_refresh` provisions an Authoring
successor with a draft DataVersion. Fresh schema, access, company context,
capture plans, and protected filters are bound to that successor.
`OdooCapturePublicationService` publishes complete immutable Parquet and origin
evidence, then `WorkspaceDataVersionSourceService.accept_odoo_capture` freezes
the canonical source package. The saved repository advances its version pointer
only for a complete set. Operation receipts and optimistic revisions prevent
retries or concurrent edits from replacing accepted data with a partial result.

Atomic local publication is distinct from Odoo capture consistency. Paginated
API reads observe a bounded interval. Membership, schema, context and available
change checks detect observed drift, but cannot provide a transactionally
simultaneous database snapshot or detect every change that reverts between
checks. Capture start and finish evidence remains retained.

### Fresh restore ownership and field decisions

Every restoration uses a fresh run and workspace over the selected frozen
DataVersion. `OdooSavedDataProjectionRepository` checks matching project and
DataVersion ownership, source selection and manifest hashes, and artifact
integrity before attaching immutable caches. It reuses the DataVersion-owned
Parquet and protected origin artifacts without copying previous destination
approvals or execution journals.

`SavedOdooRestoreBinding` records the source owner and a fresh generation.
This dedicated admission permits a reviewed restore after a reset at the same
server address and database name. Connection identity alone does not prove
database generation. Ordinary Odoo-source transfer protections still apply
outside this retained-data entry point, and an old interrupted journal cannot
authorize recovery against the replacement database.

`restore_saved_odoo_data` resolves the destination company with fresh access,
captures destination metadata, and calls `compile_odoo_restore` for all selected
models. The pure domain plan classifies captured fields, schema changes,
creation requirements and field policies. Matching keys cannot be excluded
or renamed through an ordinary field policy. Required related values still
need an admitted existing or incoming provider; a numeric scalar default does
not supply a relationship.

The consolidated review retains explicit exclusions and replacement field
names, reports incompatible types and missing models, and accepts reviewed
scalar defaults for new records. `apply_odoo_restore` derives each row's effective
write and verification values. Keep policies preserve current values; fill-empty
policies preserve meaningful false and zero values. Many2many replacement,
keep and additive policies distinguish record membership from record deletion.
Destination-only records and extra children are preserved.

The **How matching records are found** section also offers **Match within**
for admitted captured Many2one relationships. A saved destination profile
retains `settings_json.scopes` as a model-to-selected-field mapping alongside
its scalar keys. The composition passes those fields to the same
`DestinationMatchKeyChoice` used by ordinary transfer. Selected scope fields
are identity inputs to `compile_odoo_restore`; field policies cannot rename,
exclude or keep a different destination value for them.

The admitted scope is one direct writable stored relationship to a selected
unscoped parent that already matches exactly one destination record. Null,
missing, ambiguous or recursively scoped parents, and inbound links to the
scoped model, remain blocked. This matching scope does not itself remap source
companies; explicit reference aliases supply that separate decision. The
[load workflow](06-load-into-odoo.md#direct-existing-parent-matching-scope)
owns the shared portable identity, matching, execution and recovery boundary.

**Use different destination values** remembers an explicit alias for an
unscoped existing-only reference dataset with one scalar key. The shared
matching engine validates the included source identity, target uniqueness,
access and non-collapsing population on every restore. This permits a saved
`Company A` reference to use existing `Company B`, including the admitted native
`account.account.company_ids` Many2many surface, without changing the source or
writing Company records. It does not establish a complete accounting backup.

[`saved_identity_alias_rule_owner`](../../../src/impodo/web/composition/saved_odoo_restore.py)
binds remembered rules to project, saved set, source database/company/major,
profile, destination company and connection, and chosen keys/scopes.
`settings_json.identity_alias_rules` stores only a protected reference containing
`rules_id`, `rules_hash`, `artifact_hash` and `owner_key`. Exact values belong
to the separately encrypted reusable-rule artifact exposed through
[`DestinationIdentityAliasStore`](../../../src/impodo/application/transfer_identity_alias_ports.py).
[`saved_identity_alias_rules`](../../../src/impodo/web/composition/saved_odoo_restore.py)
authorizes before opening those rules; profile-revision checks reject stale
forms before decrypting. Unavailable rules require explicit replacement before
a check can save replacement values or deliberately clear them.

Each fresh restore revalidates the rules against its own data and destination
and emits new per-read evidence. It carries no prior write approval. The
[shared alias boundary](06-load-into-odoo.md#reviewed-values-for-existing-reference-records)
owns review, preflight, compilation and retained recovery semantics. Independent
fresh-key tests establish controlled rebinding; they are not a complete
archive-import-and-restore qualification.

The composition continues through destination matching, dependency order,
exact transfer review, fresh preflight and `TransferExecutionService`.
Only current matching contract 18 and review contract 9 are admitted. Retired
runtime checks and reviews require a fresh destination check and newly built
and approved review; this does not retire or rewrite the retained source data.
The submitted confirmation authorizes that selected version, destination and
field policy set. Changed evidence stops execution. The shared writer journals
attempts and independently reads back the effective policy result. There is
no transaction spanning the complete restore and no whole-restore rollback.
Recovery uses the retained execution snapshot without contacting source Odoo.

### Portable archives and retention

`OdooSavedDataArchiveService` authorizes export of the exact saved version and
its historical configuration. The archive backend selects explicit schema,
company, selector, filter, scope, Parquet and origin members. It never exports
workspace databases, source credentials, original encryption keys, destination
profiles, or prior write authorization.

The package codec authenticates the encrypted container before exposing its
members. The payload validator checks its closed metadata shape, complete
model and relationship coverage, selector and snapshot bindings, Parquet
hashes and physical schema, row counts, filters, and protected origins before
creating imported roots. Import is project-authorized before provisioning.
The UI limits uploads to 100 MiB; the codec separately bounds member counts,
member bytes and total bytes.

Import creates new DataVersion, selector, dataset, run and workspace identities.
Stable model and field column keys allow identical Parquet bytes to be
preserved. Protected filter values are rebound to the new project and data
version. `prepare_capture_origins` encrypts original source IDs, write dates
and relationship columns with the new DataVersion key. New manifests retain
the original capture interval while assigning local retention. Package-bound
operation identities let a repeated import return its existing saved version.

The saved registry pins complete DataVersions. The protected-provenance
retention callback checks those pins after authorization, so ordinary expiry
or successful-load cleanup does not remove a reusable restore point.
Explicit project deletion remains a separate authorized lifecycle action.
Progress jobs are process-local; accepted versions, operation receipts and
execution journals are durable evidence.

Local page reads and commands use bounded synchronous scopes. Source and
destination network calls and package streaming run after local owners close.
Model-level batching and columnar snapshots remain the reuse boundary; do not
add a source-record loop that opens DuckDB or reads metadata per row.

## Contract invariants

Connection and capture checks use the shared operation policy in
[`domain/odoo/compatibility.py`](../../../src/impodo/domain/odoo/compatibility.py).
The adapters cross-check structured version information when Odoo provides it.
An official final package may append a valid calendar build date, such as
`20.0-20261015`, while its release tuple reports `20.0`. Recognition retains
the exact reported string and accepts this pair only when the release and
edition agree. Other conflicts still block connection and capture.
Unknown, malformed, or contradictory evidence cannot authorize capture. Odoo
19 retains its existing acceptance rules. Final Odoo 20 enables connection,
schema capture, bounded source capture, comparison reads, Recipe authoring,
loading, recovery, and qualified-plan Production. The live Community
and Enterprise qualification details remain available in Git history.

Target evidence is either verified `LIVE_API` capture or an unverified
`LOCAL_MANUAL` draft. A manual draft may support mapping work but cannot
authorize mapping submission. Abstract and transient models are excluded, and
related models are never silently added to the permitted scope.

Supporting-model derivation is generic and limited to direct captured
required relationship fields outside the selected write scope, plus
one-to-many child data with a captured inverse. It excludes non-exportable and
company-dependent fields, does not recursively traverse the Odoo model graph,
and never changes `intended_models`. Optional many-to-one and many-to-many
fields wait for accepted-source or Recipe intent in a later slice. A
one-to-many with a captured inverse is presented as related incoming business
data; computed, related, read-only, and inverse-managed relationships remain
Odoo managed. A usable required create default remains a checked-default
decision.

A required relationship classified as reuse existing must point to a model in
the current target-bound model catalogue. Otherwise the presenter emits a
current **Must fix** issue and `govern_complete` rejects Stage 2 completion. An
unavailable inverse-owned child model remains a **Review** finding. This proves
model availability only; Stage 3 and later checks still own source-value
coverage, identity, ambiguity, and record-level relationship resolution.

Field capture records the effective inherited field set from the connected
Odoo major. For each field, it records requirements, read-only state,
relationships, inverse fields,
and selection codes. It performs one `fields_get` request per selected model,
then at most one `default_get` request for that model's supported required
writable fields; neither request runs per field or source row. A positive
Many2one record identity can be retained only as evidence for this exact target
context. Other relational defaults and unusable scalar values are not retained.
Impodo fetches optional uniqueness metadata in one bounded model batch. If it
cannot read that metadata, it does not present a recommendation as confirmed
governance.

Final Odoo 20 uses `uom_id` on `mrp.bom` and `mrp.bom.line`, removes
`product.template.uom_po_id`, and replaces the Odoo 19 Unit of Measure category,
rounding, and unit-type shape with `relative_uom_id`, `relative_factor`, and a
stored `factor`. The schema adapter preserves those native contracts. It does
not introduce field aliases. The Odoo 19 curated Unit name within Category
recommendation therefore does not match the Odoo 20 schema; an Odoo 20 unit
identity needs an explicit reviewed rule until a separate version-specific
recommendation is approved.

Business keys are explicit, versioned, and actor-confirmed. A recommendation
may come from one exact supported rule or one unambiguous Odoo uniqueness
constraint, but it remains non-authoritative until confirmation. Relationships
and matching use these portable keys rather than remembered numeric Odoo IDs.

`business_keys.py` owns recommendation policy version 1. It classifies a
preferred rule as `ODOO_ENFORCED` or `CURATED_CONVENTION`, and returns
`MULTIPLE_CANDIDATES` or `NO_SAFE_CANDIDATE` without a preferred rule when it
cannot choose safely. A curated rule is an exact model, field type, and relation
contract; a translated label or a field merely named `code`, `ref`, or `name`
does not qualify another model.

The current Odoo 19 matching-rule policy covers Country code, Language code,
Currency code, Contact reference, Company name, Product and Product variant
Internal Reference, Product category name within its parent, Unit name within
its category, BoM reference, BoM line sequence within its parent BoM, and Work
Center Usage operation name within its BoM. Convention rules carry a warning
when Odoo permits blanks or duplicates. In particular, BoM Component within
Parent BoM is not the default because the same component may legitimately
occur more than once; it remains available through the advanced editor.

`_schema_key_views` server-renders the preferred fields, scope, and description
when there is no submitted draft and no confirmed rule. A submitted draft wins
over the recommendation after validation failure, and an existing confirmed
rule wins over a later policy version. JavaScript updates the advanced editor,
but it is not required to place suggested values in the submitted form.

The authoring route binds confirmation to both the captured schema content hash
and the recommendation-policy version. `SchemaWorkspaceService.govern_complete`
requires exactly one confirmed rule per captured model before publishing new
Stage 2 governance. Recipe application continues to use `govern`, because its
saved Recipe already defines the required model subset. A used suggestion
stores its basis and policy version in backward-compatible governance evidence;
an advanced override stores only the reviewed matching shape. Page rendering
and confirmation remain local and make no additional Odoo call.

The representative browser qualification installs captured Odoo 19 evidence
for a standard Contact, an extended-standard Product with a custom legacy
field, and a custom Asset with an enforced unique constraint. It proves one
confirmation, override persistence after a validation error and reload, and a
visible unresolved custom-model blocker. Connector call vectors are recorded
after capture and must remain unchanged across every review and confirmation
request.

`reference_keys.py` owns separate Odoo 19 and Odoo 20 governed-reference
policies. A
captured parent relation may authorize a reviewed supporting model outside the
primary schema only for its exact key, scope, display fields, and read purpose.
The policy rejects write use, unrestricted metadata, extra fields, wrong
relations, and incompatible captured metadata. The supporting model therefore
remains outside the migration write scope.

## Code references

| Role | Code |
| --- | --- |
| Target company choice boundary | [`odoo_company.py`](../../../src/impodo/web/routers/odoo_company.py) |
| Schema orchestration | [`SchemaWorkspaceService`](../../../src/impodo/application/schema_workspace_service.py) |
| Purpose-specific connection check | [`OdooConnectionTestService`](../../../src/impodo/application/odoo_connection_service.py) |
| Schema governance | [`governance.py`](../../../src/impodo/domain/schema/governance.py) |
| Matching-rule recommendations | [`business_keys.py`](../../../src/impodo/domain/workspace/business_keys.py) |
| Supporting-model derivation | [`supporting_models.py`](../../../src/impodo/domain/workspace/supporting_models.py) |
| Source-aware dependency proposal | [`supporting_plan.py`](../../../src/impodo/domain/workspace/supporting_plan.py) and [`supporting_sources.py`](../../../src/impodo/domain/workspace/supporting_sources.py) |
| Reviewed supporting decisions | [`supporting_review_service.py`](../../../src/impodo/application/supporting_review_service.py), [`supporting_review.py`](../../../src/impodo/domain/workspace/supporting_review.py), and [`supporting_review_repository.py`](../../../src/impodo/adapters/duckdb/supporting_review_repository.py) |
| Downstream supporting constraint | [`SupportingMappingGuard`](../../../src/impodo/application/supporting_mapping_guard.py) and [`supporting_closure.py`](../../../src/impodo/domain/workspace/supporting_closure.py) |
| Shared portable Recipe requirements | [`extract_odoo_requirements`](../../../src/impodo/application/recipe/odoo_requirements.py) |
| Governed supporting references | [`reference_keys.py`](../../../src/impodo/domain/workspace/reference_keys.py) |
| Workflow issue projection | [`issues.py`](../../../src/impodo/application/workspace/issues.py) |
| Browser routes | [`schema.py`](../../../src/impodo/web/routers/schema.py) |
| Browser presenter | [`schema.py`](../../../src/impodo/web/presenters/schema.py) |
| Supporting-data presenter | [`supporting_models.py`](../../../src/impodo/web/presenters/supporting_models.py) |
| Browser review | [`workspace_schema.html`](../../../src/impodo/web/templates/workspace_schema.html) |
| Matching-rule screenshot capture | [`capture_matching_rule_screenshots.py`](../../../scripts/capture_matching_rule_screenshots.py) |
| Odoo-source eligible-field screenshot capture | [`capture_odoo_source_eligible_fields.py`](../../../scripts/capture_odoo_source_eligible_fields.py) |
| Saved source configuration and immutable version lifecycle | [`SavedOdooDataService`](../../../src/impodo/application/odoo_saved_data_service.py), [domain records](../../../src/impodo/domain/odoo_saved_data.py), and [registry persistence](../../../src/impodo/adapters/duckdb/odoo_saved_data_repository.py) |
| Independent refresh and restore workspaces | [`OdooSavedDataWorkspaceService`](../../../src/impodo/application/odoo_saved_data_workspaces.py) and [immutable source projection](../../../src/impodo/adapters/duckdb/odoo_saved_data_projection.py) |
| Source company storage | [Source company command](../../../src/impodo/adapters/duckdb/odoo_saved_source_company.py) and [schema companion](../../../src/impodo/adapters/duckdb/schema/odoo_saved_source_company.py) |
| Restore policies and checked execution | [Pure restore plans](../../../src/impodo/domain/odoo_restore.py) and [restore composition](../../../src/impodo/web/composition/saved_odoo_restore.py) |
| Saved-data routes, progress, and source access | [Router](../../../src/impodo/web/routers/saved_odoo_data.py), [jobs](../../../src/impodo/application/odoo_saved_data_jobs.py), and [source composition](../../../src/impodo/web/composition/saved_odoo_data.py) |
| Portable archive lifecycle | [Archive service](../../../src/impodo/application/odoo_saved_data_archive.py), [backend](../../../src/impodo/adapters/duckdb/odoo_saved_data_archive.py), [semantic validation](../../../src/impodo/adapters/duckdb/odoo_saved_data_archive_payload.py), [import ownership](../../../src/impodo/adapters/duckdb/odoo_saved_data_archive_import.py), and [encrypted codec](../../../src/impodo/adapters/odoo_saved_data_package.py) |
| Saved-data documentation capture | [`capture_saved_odoo_data.py`](../../../scripts/capture_saved_odoo_data.py) |
| Local reader | [`local_odoo_reader.py`](../../../src/impodo/adapters/odoo/local_reader.py) |

## Evidence and state

The model catalogue records the available scope. The schema catalogue binds
models, fields, types, requirements, selections, relations, and target
provenance. It may also carry one unconfirmed refresh candidate while its
current content hash continues to identify the published schema. In file mode,
`SchemaGovernance` binds the confirmed business-key rules to that exact schema
revision.

Stable technical model and field names are evidence; translated UI labels are
presentation. Numeric database IDs must not become portable identities.
Supporting groups and workflow issues are derived projections of the current
schema and model catalogue, not a second mutable evidence store.

## Completion and navigation

File mode completes only when both the schema catalogue and schema governance
exist and no required reuse dependency is absent from current model discovery;
it then unlocks Match data. Odoo source mode completes its first responsibility
when the eligible schema exists, then unlocks the bounded capture and freeze
responsibility.

The captured file-mode target is fresh evidence owned by the current
DataVersion. Optional Recipe publication can compile required portable Odoo
semantics, but the server identity, schema capture, and credentials never
become Recipe meaning. Project-owned application planning belongs to the
integrated Test workflow.

## Invalidation and recovery

An unchanged schema check does not invalidate dependent evidence. Detecting a
change preserves the current revision while the data manager reviews the
candidate. Confirming that candidate invalidates dependent mapping revisions
and later artifacts. Governance changes retain their existing invalidation
boundary. Local draft capture is a deliberate development path and may not be
presented as live Odoo evidence. Connector failures must retain the upstream
cause instead of being reduced to a generic browser status.

When field capture detects that the saved model catalogue belongs to older
read access, `SchemaWorkspaceService` raises
`OdooModelCatalogRefreshRequired`. The schema reader refreshes model discovery
once and repeats the field read with fresh identity verification. Both the
browser request and Recipe background job use this recovery. Model discovery
keeps the selected scope and current schema evidence. An existing live schema
still uses candidate comparison and explicit confirmation of changes.

Only this stale-catalogue failure triggers recovery. Authentication, transport,
target, and field-validation failures retain their normal handling. If access
changes again during the retry, the operation stops and retains the current
schema and saved choices.

## Odoo compatibility and performance

Model selection and matching-key confirmation run their complete local command
and recovery rendering through `run_local_operation`. Metadata capture and
model discovery publish in bounded synchronous local phases that close before
Odoo reads and before a retry. Capturing metadata still checks the same access,
target, source, and stale-evidence bindings. Both Odoo 19 and Odoo 20 retain
local and remote access coverage.

The [stage performance regressions](../../../tests/integration/web/test_stage235_performance.py)
exercise successful confirmation and rejected
matching keys, including unchanged evidence after rejection. They bound SQL
statements as well as physical opens. The shared schema counter now includes
linkage and Data version store checks; earlier engine-only measurements below
record the counter coverage of their original build.

Opening `/workspaces/{workspace_id}/schema` renders saved evidence through
`run_workspace_page_read`. The worker retains database handles for that page read and
closes them before returning. This avoids repeated database opens and keeps
the event loop responsive to other requests. Opening the page does not call
Odoo or refresh evidence; capture and change checks remain explicit actions.

The route passes its
[`SourceReviewPage`](../../../src/impodo/application/browser_queries.py)
into the schema presenter, so the
presenter reuses the workspace state and source package that the route verified.
Each request still checks source lineage and package hashes. The access
middleware supplies an admission callback that resolves authorization and
applies its local route policy before page construction in the same bounded
worker. The isolated fixture opens each of its four database owners once:
registry, Workspace linkage, Workspace engine, and Data version.

The route also passes one authorized
[`RunSetupPage`](../../../src/impodo/application/run/setup_service.py)
to the presenter. Routing, Odoo requirements, and Test recovery reuse its
selection, including an absent setup.
[`RunOdooRequirementsUseCase.for_selection`](../../../src/impodo/application/run/odoo_requirements.py)
retains Project access and selected Recipe hash checks. Active Test recovery
uses a [scalar required-default blocker count](../../../src/impodo/adapters/duckdb/migration_run_planning_repository.py)
instead of loading application bundles and complete issues. This display
count grants no recovery authority; full evidence checks remain in command
and review readers. Production retains its
qualified Cutover plan check and does not enter Test recovery.

The schema presenter shares its safe read-credential status with the read-key
dialog only when the resolved credential owner and target are identical.
Another owner requires a separate status read. Status is discarded after the
request; capture and refresh commands still retrieve and validate current
credentials independently.

Database schema checks use
[`read_table_columns`](../../../src/impodo/adapters/duckdb/schema/structure.py)
to read ordered columns in one catalogue query instead of
issuing a query per table. Each check remains fresh, including after a schema
change on the same connection. Retaining database owners does not cache
verification or combine repository transactions across stores.

Supporting-model derivation is one bounded pass over the captured fields plus
local catalogue joins. It performs no recursive graph walk and adds no network,
per-model, per-field, or per-row request.

The separate source-aware proposal query makes four bulk application reads,
independent of table row counts or dependency-edge counts. In the composed
local fixture, one retained read operation opens the registry, canonical
Workspace, Data version, and workspace engine once each. Its regression test
requires four physical connections, at most four schema checks, and zero lock
retries. This is structural fixture evidence, not a live latency claim. The
planner makes no database or Odoo call and adds no work to the current page.

The reviewed-decision page adds two bulk reads, independent of relationship
count, within the existing retained page operation. The action-rich fixture
requires at most six physical connections, at most six schema checks, and zero
lock retries. Rendering and saving perform no Odoo read. Two reviewed edges
using Countries share one metadata/record request group and one lookup
publication. The capture fixture proves that no retained DuckDB owner crosses
the Odoo boundary and that a schema change during the read publishes no old
lookup. These are controlled fixture results, not live latency measurements.

Read capability is explicit and narrow: model catalogue, metadata, target
fingerprint, and planned record requests. Batch metadata and record reads by
model. Never call `fields_get`, selection providers, or relationship catalogues
inside a source-row loop.

A schema check performs the same bounded metadata read as an initial capture:
one `fields_get` request and at most one `default_get` request per selected
model, plus the bounded constraint batch. Candidate comparison and confirmation
run locally. Confirmation does not call Odoo again and introduces no per-field
or per-row requests.

A stale-catalogue recovery adds at most one model-discovery read and one repeat
of the bounded field check. A normal field check adds no model-discovery call.
The model-discovery read and its persistence run in the synchronous worker.

Odoo 19 inherited fields and dynamic selections must come from the connected
database. Do not hard-code a standard-only catalogue when custom modules are in
scope.

## Verification

- [Restore field, membership, schema and value policies](../../../tests/domain/test_odoo_restore.py).
- [Saved definitions, immutable versions, profiles and operation persistence](../../../tests/integration/duckdb/test_odoo_saved_data_repository.py).
- [Offline restore projections, retry and source-company ownership](../../../tests/integration/web/test_saved_odoo_workspace_projection.py).
- [Source-company forward upgrade and protected hash checks](../../../tests/integration/duckdb/test_saved_source_company_upgrade.py).
- [Restore policies through transfer matching and execution compilation](../../../tests/application/workspace/test_saved_odoo_transfer.py).
- [Direct existing-parent matching and execution](../../../tests/application/workspace/test_scoped_odoo_transfer.py) and [ordinary and saved scope-choice protection](../../../tests/integration/web/test_transfer_matching_scope_choices.py).
- [Saved scoped field policies, fresh preflight, compilation and independent reconciliation](../../../tests/application/workspace/test_scoped_saved_odoo_transfer.py) cover both majors and local/remote fictional metadata. Actual Odoo writes and a complete browser restoration remain separate qualification.
- [Saved reference aliases, scoped parents, native account company membership, recovery and fresh-key rebinding](../../../tests/application/workspace/test_aliased_saved_odoo_transfer.py), [encrypted rule storage](../../../tests/integration/protected_evidence/test_destination_identity_aliases.py) and [saved-profile forms](../../../tests/integration/web/test_saved_identity_alias_choices.py) cover the bounded alias capability. Raw rules stay out of profile settings and public review evidence.
- [Encrypted package format and rejection of malformed containers](../../../tests/integration/artifacts/test_odoo_saved_data_package.py).
- [Independent-installation archive round trips](../../../tests/integration/web/test_saved_odoo_archive_lifecycle.py) cover Odoo 19 and 20 with local and remote source metadata, new keys, filter rebinding, no source access, retry, malformed metadata, and authorization rejection. These are controlled fixtures; they do not qualify live ORM behavior or a complete Chart of Accounts.
- [Saved-data browser routes and destination review](../../../tests/integration/web/test_saved_odoo_data.py), [initial and repeated source capture](../../../tests/integration/web/test_saved_odoo_data_refresh_lifecycle.py), and [checked restoration through the assembled application](../../../tests/integration/web/test_saved_odoo_restore_flow.py).
- [Assembled saved-alias restoration](../../../tests/integration/web/test_aliased_saved_restore_flow.py) runs one method with four Odoo 19/20 and local/remote cases. Each captures two models into encrypted provenance and Parquet, retains protected profile choices, disables source access, then restores and independently verifies twice through the shared execution flow and a fictional write adapter. Eight verified restores retain stable destination record IDs without duplicate creates. A repeat may reassert writes. This does not qualify live ORM behavior, independent package import or a same-endpoint database reset.

Fresh saved-data page screenshots were not captured during this implementation:
the execution sandbox rejected browser Mach registration, and native browser
access timed out. The user guide's illustration shows the existing verified
source-field selection step. It is not presented as visual verification of the
new saved-data page. The reproducible capture script remains available for an
environment that permits browser capture.

- [Supporting closure and conflicting business choices](../../../tests/domain/workspace/test_supporting_closure.py)
- [Portable supporting decisions in Recipes](../../../tests/domain/recipe/test_supporting_decisions.py)
- [Consumer freshness and bulk metadata reads](../../../tests/application/workspace/test_supporting_mapping_guard.py)
- [Lookup freshness across scoped permission evidence](../../../tests/application/workspace/test_supporting_lookup_freshness.py)
- [Primary and supporting remote-access probes](../../../tests/integration/web/test_supporting_read_access.py)
- [`tests/integration/duckdb/test_workspace.py`](../../../tests/integration/duckdb/test_workspace.py)
- [`tests/integration/odoo/test_local_reader.py`](../../../tests/integration/odoo/test_local_reader.py)
- [`tests/application/workspace/test_odoo_connection.py`](../../../tests/application/workspace/test_odoo_connection.py)
- [`tests/application/workspace/test_schema_governance.py`](../../../tests/application/workspace/test_schema_governance.py)
- [`tests/domain/workspace/test_business_keys.py`](../../../tests/domain/workspace/test_business_keys.py)
- [`tests/domain/workspace/test_supporting_models.py`](../../../tests/domain/workspace/test_supporting_models.py)
- [`tests/domain/workspace/test_supporting_plan.py`](../../../tests/domain/workspace/test_supporting_plan.py)
- [`tests/domain/workspace/test_supporting_sources.py`](../../../tests/domain/workspace/test_supporting_sources.py)
- [`tests/application/workspace/test_supporting_data_plan.py`](../../../tests/application/workspace/test_supporting_data_plan.py)
- [`tests/application/workspace/test_supporting_review.py`](../../../tests/application/workspace/test_supporting_review.py)
- [`tests/integration/web/test_supporting_review.py`](../../../tests/integration/web/test_supporting_review.py)
- [Company-list policy boundaries](../../../tests/domain/execution/test_company_reference_policy.py)
- [Local and remote account company review](../../../tests/integration/web/test_account_company_supporting.py)
- [Constant company chooser and parent-field placement](../../../tests/integration/web/test_constant_company_list.py)
- [Singleton company preparation and batched reads](../../../tests/domain/preparation/test_constant_list_relationships.py)
- [Constant company-list mapping in the browser](../../../tests/integration/web/test_constant_company_list.py)
- [Constant list relationship preparation](../../../tests/domain/preparation/test_constant_list_relationships.py)
- [Account company Recipe publication and rebinding](../../../tests/domain/recipe/test_account_company_references.py)
- [`tests/application/workspace/test_workflow_issues.py`](../../../tests/application/workspace/test_workflow_issues.py)
- [`tests/integration/web/test_schema_presenter.py`](../../../tests/integration/web/test_schema_presenter.py)
- [`tests/integration/web/test_stage12_page_loading.py`](../../../tests/integration/web/test_stage12_page_loading.py)
- [`tests/application/run/test_setup_page.py`](../../../tests/application/run/test_setup_page.py)
- [`tests/application/run/test_odoo_requirements.py`](../../../tests/application/run/test_odoo_requirements.py)
- [`tests/integration/duckdb/test_schema_structure.py`](../../../tests/integration/duckdb/test_schema_structure.py)
- [`tests/integration/web/test_stage2_supporting_models.py`](../../../tests/integration/web/test_stage2_supporting_models.py)
- [`tests/integration/web/test_target_workflow.py`](../../../tests/integration/web/test_target_workflow.py)
- [`tests/integration/web/test_schema_capture_recovery.py`](../../../tests/integration/web/test_schema_capture_recovery.py)
- [`tests/e2e/test_stage2_matching_rules.py`](../../../tests/e2e/test_stage2_matching_rules.py)

Verify inherited fields, selection normalization, business-key revisioning,
read-only capability, batched requests, invalidation, and both source modes.

## Related documentation

- [User guide: save and restore clean Odoo data](../../user/guides/saved-odoo-data.md).
- [Workflow responsiveness qualification](../../plans/09-responsive-workflow-liveness-and-navigation.md)
- [User guide: Odoo data](../../user/workflow/02-odoo-data.md)
- [Workflow evidence lifecycle](../contracts/evidence-lifecycle.md)
- [Platform support and testing](../reference/platform-support-and-testing.md)
- [Architecture decisions](../../decisions/README.md)
