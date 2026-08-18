# Reusable recipes and data versions implementation plan

## Status and authority

**Status:** Proposed implementation plan from 2026-08-18. No phase is
implemented unless the repository, tests, and current documentation say so.

This document defines a scoped delivery path for reusing a confirmed Impodo
mapping and transformation recipe with later replacement files inside one
business migration project. It does not replace the priority order in
[Impodo remaining work](remaining-work.md), weaken the
[workflow evidence lifecycle](../developer/contracts/evidence-lifecycle.md),
or describe source replacement as current behavior.

The first supported profile is intentionally narrow:

- the source is a complete replacement set of CSV/XLSX files;
- the business purpose and Odoo 19 target remain the same;
- the latest confirmed mapping becomes an immutable reusable recipe;
- the operator starts a new data version under the same visible project;
- Impodo rebinds compatible rules and asks the operator to review only drift;
- every new data version receives fresh source, preparation, comparison,
  approval, and execution evidence.

Delta files, automatic deletion for records absent from a later file, sharing
recipes between unrelated projects, and unattended recurring execution are
out of scope for the first release.

## 1. Outcome

An operator who has completed a customer migration rehearsal can return two
weeks later, choose **Use new files**, upload the updated export, and retain the
confirmed business rules without recreating the project from scratch.

Impodo must:

1. keep the earlier files, rules, preparation, review, and load evidence
   immutable and reopenable;
2. create a fresh contained workspace for the new data version;
3. apply the latest stored recipe only after the new source and current Odoo
   schema are known;
4. bind recipe datasets and columns to the new physical dataset identities;
5. reuse transformations, business keys, value matches, relationship rules,
   derived-table rules, validations, and control totals when compatible;
6. surface new or stale values, columns, target fields, and relationships as
   targeted recovery actions;
7. prevent mapping submission, preparation, or loading while a required
   exception remains unresolved; and
8. create a new immutable recipe revision when the corrected mapping is
   confirmed.

The normal result should read like:

> Recipe applied: 47 rules reused. Two new values need attention.

The operator should not have to reopen the 47 compatible rules.

## 2. Current repository boundary

The current design correctly protects one project's evidence, but it does not
yet provide a reusable-recipe boundary:

- `ProjectRepository` creates one UUID-contained directory and DuckDB database
  for each project. Source, mapping, preparation, quality, comparison, and load
  pointers live in that database.
- `source_selection`, `mapping_working_draft`, `mapping_current`, and several
  downstream tables use singleton current pointers inside one project.
- adding or removing a source file fails once `source_selection` exists.
- a `MappingDefinition` binds the exact source-selection hash and Odoo schema
  hash.
- physical dataset IDs include the registered file ID and table key. Column
  stable keys include the source name and ordinal. A later upload therefore
  cannot safely receive the previous raw dataset IDs.
- value matching already stores portable source and target strings rather than
  numeric Odoo IDs, and it supports scalar selections plus eligible
  single-key relationships.
- the central `registry.duckdb` stores lightweight project summaries but has no
  project-series, data-version, or recipe lineage.

The feature must not replace files in a frozen project, mutate old mapping
JSON, copy a project database, or edit project storage directly. It must create
new evidence and explicitly rebind portable meaning.

## 3. Product vocabulary

### 3.1 Project

The business migration scope visible to the operator, for example **Customer
migration**. A project groups its data-version history and current recipe.

### 3.2 Data version

One immutable source package and its complete workflow evidence. Examples are
**Data version 1 - rehearsal** and **Data version 2 - 31 August export**.

Each data version uses a distinct `project_id` and contained DuckDB workspace
internally. This preserves the existing singleton and invalidation contracts
while the browser groups those workspaces under one visible project.

### 3.3 Recipe

Portable, versioned business meaning that can be rebound to another compatible
data version. A recipe revision is append-only and content-hashed. It is not a
source snapshot, target snapshot, preparation result, approval, or permission
to write Odoo.

### 3.4 Recipe application

The deterministic assessment that binds one recipe revision to one data
version's current source selection and Odoo schema. It produces either a new
mapping working draft or a bounded list of issues that must be resolved first.

## 4. Architecture decision

### 4.1 Use a project series with clean child workspaces

Keep the current one-database-per-`project_id` boundary. Add registry-level
series and edition metadata so several contained workspaces appear as one
business project.

This is preferred over adding `data_version_id` to every project table because
the current schema contains many singleton pointers and hash-bound downstream
artifacts. Retrofitting all of them would create a large cross-stage migration
and increase the risk that old evidence accidentally satisfies a new run.

It is also preferred over cloning the prior DuckDB database. A database clone
would copy old source rows, current pointers, approvals, protected evidence,
execution journals, and target-specific state that must not carry forward.

### 4.2 Store recipes outside edition workspaces

Recipe revisions belong to the project series, not to one edition. Extend the
registry storage boundary with recipe metadata and bounded canonical recipe
JSON. The payload is portable but may contain confidential business literals
or value matches, so it inherits the project's data classification and must
never be exposed across a series.

Do not store source rows, distinct-value scans, credentials, Odoo numeric IDs,
protected provenance, or row-level compatibility issues in the registry.

### 4.3 Keep application results project-local

The successful application materializes a normal `MappingWorkingDraft` in the
new edition, bound to its current source-selection and schema hashes. The
application audit event records recipe ID, version, and content hash.

Structural compatibility issues are recalculated from the immutable recipe,
source snapshots, and schema snapshot until they are resolved. Categorical
coverage issues join the normal mapping validation result and its validation
hash. No second mutable mapping representation is introduced.

## 5. Recipe contract

Introduce a strict `RecipeDefinition` contract independent of
`MappingDefinition`. It must exclude instance-specific IDs and hashes while
retaining enough information to create a new exact mapping.

### 5.1 Required top-level fields

- recipe contract version;
- logical datasets in deterministic order;
- logical derived-dataset rules;
- target model bindings and migration modes;
- business-key and scope definitions;
- scalar providers, transformations, validation rules, and comparison policy;
- relationship resolvers, key/scope mappings, and missing/ambiguous policies;
- target-field dispositions and approved write fields;
- value matches and unmatched-value policies;
- business control totals; and
- source-structure expectations used for compatibility, not physical identity.

The content hash is calculated from canonical semantic content. It excludes
recipe ID, version, author, timestamp, origin project, source-selection hash,
schema hash, mapping ID, and mapping version. Confirming unchanged meaning must
reuse the existing current recipe revision rather than append a duplicate.

### 5.2 Logical dataset and column bindings

A physical recipe dataset stores:

- the operator-confirmed dataset name;
- expected source kind (`FILE` in the first release);
- selected-table hints such as prior display name, header row, and exact header
  signature;
- logical recipe-column IDs;
- exact prior source names;
- prior ordinals for diagnostics and duplicate-name disambiguation;
- expected candidate types; and
- whether each column is required by identity, mapping, relationship,
  derivation, validation, or control-total rules.

Recipe rules refer to logical recipe dataset/column IDs. They never refer to a
registered `file_id`, physical `dataset_id`, table key, source hash, snapshot
path, or current column stable key.

Application matching is deliberately conservative:

1. match datasets by the operator-confirmed dataset name;
2. match columns by exact source name when the name is unique;
3. permit reordered columns after an exact unique-name and compatible-type
   match;
4. use the prior ordinal only to disambiguate repeated exact names;
5. never fuzzy-match renamed columns without confirmation; and
6. report new unused columns as information rather than silently mapping them.

### 5.3 Target bindings

Recipes retain Odoo technical model and field names, field roles, business-key
order, scope, and portable selection/business-key values. They do not retain
numeric record IDs or claim that the prior schema remains current.

Application resolves those names against a newly captured Odoo 19 schema. An
unchanged semantic dependency may be rebound even when the complete schema
hash changed elsewhere. Missing, readonly, type-changed, relation-changed, or
newly required dependencies block the affected recipe application.

### 5.4 Categorical coverage policy

Add an explicit policy wherever categorical values can be matched. Current
partial value mappings otherwise allow an unknown source value to continue as
its original text, which is unsafe for a reusable categorical recipe.

The domain distinction should be:

- `EXACT_TARGET_VALUE`: the transformed source value must already be a valid
  current Odoo selection code;
- `EXPLICIT_VALUE_MATCH`: every nonblank distinct source choice must have a
  confirmed portable source-to-target match;
- `EXACT_BUSINESS_KEY`: a relationship value is resolved directly through the
  governed target key/scope and must resolve exactly once; and
- `EXPLICIT_KEY_MATCH`: every nonblank distinct source choice must first map to
  a portable target business-key value, which must then resolve exactly once.

Blank handling remains separate. Required fields, null policy,
`on_missing`, and `on_ambiguous` continue to decide whether a blank or failed
relationship is blocking.

New authoring should default scalar selections and choice-like relationships
that use **Match values** to explicit coverage. Existing mapping contract
versions retain their current behavior until the operator confirms the new
policy. Implement this as a new mapping contract version with strict parsing;
do not reinterpret stored version 8-10 JSON in place.

### 5.5 Recipe provenance

Each immutable revision records outside its semantic hash:

- `recipe_id`, version, and parent version;
- series ID;
- content hash and contract version;
- originating project/data-version ID;
- originating mapping ID, version, and content hash;
- originating derived-plan and governance hashes where applicable;
- actor issuer, stable subject, display name, and timestamp; and
- a bounded reason such as `MAPPING_SUBMITTED` or `EXISTING_PROJECT_BOOTSTRAP`.

Only a successfully submitted, current mapping may publish a reusable recipe
in the first release. A stale or unsubmitted working draft cannot become the
series recipe.

## 6. Registry and persistence changes

Add an additive, versioned registry migration. Do not change the existing
project-database generation merely to group editions or store recipes.

### 6.1 Proposed registry tables

`project_series`

- `series_id` primary key;
- display name;
- current `project_id`;
- optimistic revision;
- created/updated timestamps; and
- data-classification projection needed to govern recipe access.

`project_edition`

- `project_id` primary key;
- `series_id`;
- positive edition number unique within the series;
- optional parent `project_id`;
- recipe ID/version applied, nullable until application;
- created timestamp and actor identity; and
- lifecycle label supplied by the operator.

`recipe_revision`

- composite primary key `(recipe_id, version)`;
- `series_id` and optional parent version;
- unique canonical content hash within the series;
- contract version;
- origin identifiers and hashes;
- actor identity and timestamp;
- byte length; and
- canonical recipe JSON.

`recipe_current`

- `series_id` primary key;
- current recipe ID/version; and
- recipe content hash.

`project_edition_creation_intent`

- operation ID and series ID;
- reserved project ID and edition number;
- parent project ID;
- state and timestamps.

The creation intent closes the cross-database/filesystem seam. Startup recovery
must finalize an existing contained workspace or remove only an empty,
unpublished workspace created by that exact intent. It must never guess at or
delete an unrelated project directory.

### 6.2 Existing-project migration

On startup, add missing registry tables and columns using explicit schema
inspection. Then backfill each existing `project_registry` row as:

- `series_id = project_id`;
- edition number `1`;
- no parent edition; and
- no current recipe until one is safely bootstrapped.

Backfill must be idempotent and must not open every project database during the
normal project-list query. Recipe bootstrap is lazy: an eligible existing
project can create Recipe v1 from its current submitted mapping when the user
chooses **Use new files** or explicitly confirms **Create reusable recipe**.

If the project has no current submitted mapping, the browser directs the user
to finish and confirm the mapping. It must not silently use a working draft.

### 6.3 Repository concurrency

- Use optimistic series revision checks for edition creation and current
  pointers.
- Serialize recipe version allocation inside one registry transaction.
- Reuse an identical current recipe by content hash.
- Reject a supplied parent recipe that is no longer current and ask the user
  to reload.
- Keep project registry synchronization and edition linkage restart-safe.
- List project series and their current editions in one set-based query; do not
  open one DuckDB database per card.

## 7. New data-version lifecycle

### 7.1 Preconditions

Show **Use new files** only when:

- the current edition is registered and readable;
- the source mode is `FILE`;
- a current submitted mapping exists;
- that mapping still matches the current source selection and schema;
- no preparation or execution job is active for the current edition; and
- the actor may view the source edition, create a project, and edit mappings in
  the new edition.

Recipe publication uses the existing mapping-submission authorization. Recipe
application uses mapping-edit authorization on the new edition. The first
release exposes no cross-project recipe catalogue.

### 7.2 Start the edition

The confirmation page states:

- previous files and outcomes remain unchanged;
- the new upload is treated as a complete replacement snapshot;
- mapping and transformation rules will be reused where compatible;
- missing old records are not deletion instructions; and
- all preparation, Odoo comparison, and load evidence will be recreated.

After confirmation, `ProjectEditionService`:

1. reserves the next edition through a creation intent;
2. creates a clean project workspace through the normal repository boundary;
3. copies non-secret project details, classification, retention, source system,
   Odoo connection settings, intended applications, and intended models;
4. links the workspace to the series and parent edition;
5. advances the series current-edition pointer; and
6. records actor-bound `DATA_VERSION_CREATED` events in the registry and new
   project audit.

It must not copy source files, catalogues, selections, snapshots, derived
results, mapping tables, schema snapshots, target record snapshots,
preparation, quality, normalization, comparisons, approvals, protected
evidence, execution snapshots, or journals.

### 7.3 Credentials

Credentials are not recipe content. Copying non-secret connection settings
does not authorize reuse of a credential.

When the prior and new connection-target hashes are identical, the browser may
offer **Reuse saved Odoo connection** as a separate explicit confirmation. A
narrow secret-store operation copies only the selected read/write role into
the new project-specific credential ID, never returns it to the browser, and
records non-secret audit events in both editions. Session-only credentials
that are no longer present cannot be reused.

If the target settings differ, credential reuse is unavailable. Target changes
continue to invalidate bindings through the existing project service.

### 7.4 Upload and freeze

The operator uploads the complete replacement source set through the existing
intake boundary. Impodo inspects each new file and presents the expected
logical datasets from the recipe as suggestions.

Exact header signatures may select a likely file table, but the operator still
confirms table choices and dataset names. The new source selection and
snapshots receive new physical IDs and hashes through the normal freeze path.
No old artifact is linked as current evidence.

### 7.5 Refresh Odoo metadata

Capture current selected Odoo 19 details before recipe application. Dynamic
selection choices, readonly/required state, relation metadata, and business-key
fields must be current.

For example, a new source language may be mapped only to a language code
available in the captured target choices. If the code is unavailable, the
operator can refresh selected Odoo details. If it remains unavailable, an Odoo
administrator must activate or configure it outside Impodo before mapping can
continue.

### 7.6 Apply the recipe

`RecipeApplicationService` performs these bounded steps:

1. load the exact current recipe revision;
2. bind logical datasets and columns to the new effective source selection;
3. rebuild derived rules against new dataset/column IDs;
4. resolve target models, fields, keys, scope, types, and relation metadata
   against the current schema;
5. construct a fresh `MappingDefinition` with a new mapping ID and the new
   source-selection/schema hashes;
6. validate its semantics through the existing mapping validator;
7. scan required categorical source columns for distinct values through a
   bounded, set-based local snapshot service;
8. add categorical coverage issues to validation evidence;
9. persist the working draft only when structural binding is complete; and
10. record `RECIPE_APPLIED` with recipe and resulting draft hashes.

The service must not contact Odoo inside a source-row loop. Relationship target
candidates are loaded in bounded model/key batches and indexed once. Source
distinct values are computed columnarly from immutable local snapshots.

## 8. Compatibility and error handling

### 8.1 Issue model

Introduce `RecipeCompatibilityIssue` for failures that occur before a valid
mapping draft can exist. Each issue has:

- stable code and deterministic fingerprint;
- severity: information, needs review, or blocker;
- logical dataset and source field labels;
- target model/field labels where applicable;
- bounded source value and affected-row count where safe;
- concise business-language explanation; and
- one explicit recovery action/URL.

Once a mapping draft exists, use canonical `MappingValidationIssue` evidence
for current-schema and value-coverage failures so the existing submission and
validation-hash boundary remains authoritative.

Do not put raw exception text, SQL, file paths, Odoo numeric IDs, or credentials
in browser messages. Log technical causes locally with correlation identifiers
where the current error boundary already supports them.

### 8.2 Compatibility issue matrix

| Condition | Proposed code | Severity | Recovery |
| --- | --- | --- | --- |
| Expected dataset absent | `RECIPE_DATASET_MISSING` | Blocker | Return to source selection and bind the correct table |
| More than one dataset can satisfy a logical name | `RECIPE_DATASET_AMBIGUOUS` | Blocker | Choose the dataset explicitly |
| Used source column absent or renamed | `RECIPE_SOURCE_COLUMN_MISSING` | Blocker | Bind the new column or correct the source |
| Repeated source name cannot be disambiguated | `RECIPE_SOURCE_COLUMN_AMBIGUOUS` | Blocker | Choose the exact column |
| Used column type changed incompatibly | `RECIPE_SOURCE_TYPE_CHANGED` | Blocker | Adjust parsing/transformation and confirm a new recipe |
| New unused source column appears | `RECIPE_SOURCE_COLUMN_NEW` | Information | Ignore or map it deliberately |
| Derived rule cannot bind an input | `RECIPE_DERIVED_RULE_UNBOUND` | Blocker | Repair the source binding before deriving rows |
| Target model/field disappeared | `RECIPE_TARGET_FIELD_MISSING` | Blocker | Refresh Odoo details or remap the field |
| Target kind, relation, readonly, or required semantics changed | `RECIPE_TARGET_FIELD_CHANGED` | Blocker | Review the affected Odoo field and mapping |
| Governed business-key field is unavailable | `RECIPE_BUSINESS_KEY_STALE` | Blocker | Refresh schema and confirm a valid key/scope |
| A source choice has no required explicit match | `MAPPING_SOURCE_VALUE_UNMATCHED` | Blocker | Open **Match values** for that field |
| A stored selection target code is no longer available | Existing `MAPPING_SELECTION_VALUE_INVALID` | Blocker | Refresh choices and select a current Odoo value |
| Relationship key has no target match | Existing missing-relationship evidence | Blocker when policy is `error` | Match another key, provide a related dataset, or create the governed master record |
| Relationship key has several target matches | Existing ambiguous-relationship evidence | Blocker when policy is `error` | Correct duplicates or add governed scope |

### 8.3 New language example

Recipe v1 contains:

- `English -> en_US`;
- `French -> fr_FR`; and
- explicit coverage for `res.partner.lang`.

Data version 2 contains `German`. Application reuses the field provider and
old matches but emits one `MAPPING_SOURCE_VALUE_UNMATCHED` issue with the
affected count. The mapping page opens directly on Language. The user selects
`German -> de_DE` from current captured choices.

If `de_DE` is absent, the mapping stays blocked and offers **Refresh Odoo
choices**. No free-text target code is accepted. After confirmation, the new
mapping submission publishes Recipe v2; Recipe v1 and Data version 1 remain
unchanged.

### 8.4 New country many2one example

Recipe v1 contains `FRA -> FR` and `BEL -> BE` for the governed unique
`res.country.code` key. Data version 2 contains `LUX`.

- `LUX` is reported as an unmatched source choice.
- Impodo may recommend `LU` only when the target key resolves uniquely.
- The operator confirms `LUX -> LU`; no numeric country ID enters the recipe.
- No target record produces a blocking missing relationship and explains that
  mapping cannot create master data by itself.
- Several target records produce a blocking ambiguity; Impodo does not choose
  one by row order or ID.

### 8.5 Failures during edition creation or application

- A stale series revision returns a conflict and leaves no second current
  edition.
- A failed workspace creation keeps the previous edition current and records
  or cleans the exact creation intent.
- A recipe parse/hash failure blocks use and preserves the stored bytes for
  diagnosis.
- A source/schema change during application aborts before saving the mapping
  draft.
- A failed categorical scan returns a retryable local-read error without
  changing recipe or mapping pointers.
- Odoo connection failures retain the new source evidence but block schema
  refresh and recipe application.
- Unknown Odoo write outcomes remain governed by the existing execution
  journal; recipe reuse never authorizes a blind retry.

## 9. Browser experience

### 9.1 Project list and overview

The project list shows one card per series, not one card per internal edition.
The card includes the current data-version label and recipe version without
opening child databases.

The overview adds a compact section:

> **Reusable recipe**
>
> Recipe v2 - ready
>
> Current data: 31 August export

- Primary action: **Use new files**
- Secondary action: **View previous data versions**

Use business labels in the browser. Keep internal terms such as
`series_id`, `project_id`, content hashes, and contract versions in audit or
technical evidence.

### 9.2 New-data confirmation

Use one confirmation page rather than extending the initial project-creation
wizard. Prefill an editable data-version label and show exactly what is kept
and recreated.

The confirmation is explicit because it creates a new retained workspace. It
does not imply an Odoo write.

### 9.3 Exception-focused mapping

After application, show one summary card above the existing mapping page:

- rules reused;
- fields needing review;
- new source choices;
- stale target choices; and
- structural blockers.

The primary action is **Review N items** and links to the first affected
mapping card or source correction. Reuse the existing **Match values** dialog
for scalar selections and eligible many2one relationships. Existing valid
cards remain collapsed/green.

All blockers must remain visible outside search and pagination. Filtering may
not hide a condition that prevents confirmation.

### 9.4 History

The history view lists, newest first:

- data-version number and label;
- received/created timestamp;
- recipe version applied;
- workflow status;
- source row summary when available; and
- a read-only link to the edition's evidence.

Historic editions never become current merely because they are opened.

### 9.5 Deletion and retention

For the first release, the normal project delete action operates on the whole
series and explicitly names the number of data versions and recipe revisions
that will be permanently deleted. It must enumerate and remove each edition's
credentials, keys, jobs, artifacts, project directory, registry row, series
metadata, and recipe history through existing governed deletion services.

Independent historical-edition purge is deferred until recipe provenance,
retention, current-pointer recovery, and credential cleanup have a complete
contract. Do not expose a partial delete button first.

## 10. Security, governance, and Odoo 19 rules

- Recipes contain no credentials, session tokens, file paths, source hashes,
  source rows, or numeric Odoo record IDs.
- Formula and pattern rules retain the existing allowlists and bounds. Recipe
  application never evaluates arbitrary code.
- Recipe reads and writes are series-scoped and actor-authorized.
- Recipe payloads inherit project classification and local filesystem
  protections. Browser responses include only the selected series.
- Every publication and application records stable actor identity.
- A recipe revision is mapping configuration, not approval or execution
  authorization.
- Odoo 19 models and fields are addressed by technical names captured through
  the current metadata boundary.
- Dynamic selections are refreshed from the selected Odoo target before load.
- Relationship resolution uses governed business keys and scope, never
  numeric IDs or display names alone.
- Target reads are planned and batched by model/key. No metadata lookup,
  connector call, or database query is permitted inside a source-row loop.
- Odoo writes remain in the existing closed writer and journal boundary. This
  feature adds no generic RPC surface and performs no write during recipe
  creation or application.

## 11. Performance contract

Recipe reuse must remove repeated authoring, not introduce row-wise work.

- Project-series listing uses one registry query.
- Recipe extraction walks mappings and rule graphs once.
- Dataset/column compatibility uses indexed dictionaries keyed by logical name
  and exact source name.
- Distinct categorical values are computed by a set-based DuckDB/Polars scan
  of the required frozen columns only.
- Target selection choices come from the captured schema snapshot.
- Many2one candidate keys are retrieved in bounded batches and indexed once
  per model/key/scope combination.
- Duplicate and missing relationship checks reuse run-scoped indexes during
  preparation.
- The existing value-mapping contract maximum remains enforced; raising it is
  separate performance work.
- Repeated compatibility views may cache only hash-bound results keyed by
  recipe hash, effective source-selection hash, and schema hash. Any dependency
  change invalidates the cache.

Measure recipe extraction/application separately from full preparation. A
compatible recipe application should scale with datasets, columns, rules, and
distinct governed choices rather than total source rows, apart from the
columnar distinct-value scan.

## 12. Implementation sequence

### Phase 0 - Freeze contracts and acceptance fixtures

- Add customer fixtures for Data versions 1 and 2 with added, changed,
  unchanged, and absent business keys.
- Add new `German` language and `LUX` country values.
- Add stale target-choice, missing country, ambiguous custom many2one,
  reordered-column, renamed-column, type-drift, and new-unused-column cases.
- Decide exact recipe and categorical-policy JSON shapes and size limits.
- Record baseline project-list, mapping submission, value-choice, preparation,
  and comparison behavior.
- Add an architecture decision note if implementation changes the selected
  registry/child-workspace design.

**Gate:** domain examples serialize deterministically and every expected
recovery action is agreed before persistence or browser work.

### Phase 1 - Add series and recipe persistence

- Add additive registry schema/version handling and idempotent existing-project
  backfill.
- Implement `ProjectSeriesRepository` and `RecipeRepository` with optimistic
  concurrency, content-hash validation, and bounded JSON.
- Add immutable recipe revision/current-pointer contracts.
- Add edition-creation intents and startup recovery.
- Change project list queries to return one set-based series projection.
- Preserve current single-edition routes through the current `project_id`.

**Gate:** all existing projects appear as one-edition series without opening
their contained databases in the list query; interrupted creation recovery is
deterministic.

### Phase 2 - Extract and publish recipes

- Implement deterministic conversion from the current source selection,
  derived plan, schema governance, and submitted mapping to
  `RecipeDefinition`.
- Replace physical dataset/column IDs with logical recipe bindings.
- Reject numeric IDs, secrets, stale evidence, unsupported mapping contracts,
  and incomplete submissions.
- Publish or reuse the series recipe within the successful mapping-submission
  workflow.
- Add lazy **Create reusable recipe** bootstrap for eligible existing projects.
- Record recipe provenance and audit.

**Gate:** two semantically identical mappings over different file IDs produce
the same recipe content hash; changing one transformation/value match changes
it.

### Phase 3 - Create clean data versions

- Implement `ProjectEditionService` and the new-data confirmation route.
- Copy only approved non-secret setup fields into a fresh project workspace.
- Link the new workspace to its series and parent edition.
- Add explicit, same-target credential reuse with audit, or prompt for a new
  credential when reuse is unavailable.
- Add series-aware project deletion orchestration without weakening current
  path containment and cleanup behavior.
- Expose history and current-edition navigation.

**Gate:** creating Data version 2 leaves every Data version 1 database byte and
artifact unchanged, and failure cannot switch the current pointer early.

### Phase 4 - Rebind recipe structure

- Implement exact dataset/column compatibility and deterministic issue
  fingerprints.
- Rebuild derived entity rules against the new dataset/column IDs.
- Rebuild mapping datasets, scalar fields, relationships, identities, scope,
  dispositions, write fields, and control totals.
- Validate current Odoo model/field semantics.
- Persist a fresh mapping working draft only after complete structural binding.
- Add `RECIPE_APPLIED` audit with old recipe and new draft hashes.

**Gate:** compatible reordered columns rebind; renamed, missing, ambiguous, or
incompatible columns fail closed with a direct recovery action.

### Phase 5 - Add categorical coverage and exception recovery

- Add the new mapping contract version and strict categorical coverage enum.
- Extract `_source_value_choices` from the web helper into a bounded
  application service over immutable snapshots.
- Extend mapping validation orchestration with explicit source-choice coverage
  issues that participate in validation hashes.
- Preserve current Odoo selection validation.
- Reuse the Match values UI for new scalar/many2one choices.
- Link each issue to the affected mapping field and keep blockers visible
  outside filters/pagination.
- Publish Recipe v2 only after the corrected mapping is submitted.

**Gate:** `German` and `LUX` block only their affected mappings, cannot pass
silently, can be mapped without editing other fields, and appear in the next
recipe revision.

### Phase 6 - Regenerate downstream evidence

- Run transformation impact, preparation, quality, normalization, Odoo
  comparison, preflight, and execution snapshot through their existing
  boundaries.
- Prove that no prior current pointer or approval satisfies the new edition.
- Confirm new/changed/unchanged Odoo outcomes through governed business keys.
- Confirm that a key absent from the replacement file never becomes an
  automatic delete proposal.
- Retain unknown-write stopping and reconciliation behavior.

**Gate:** only exact new-edition hashes reach comparison/load and the previous
edition remains independently reopenable.

### Phase 7 - UX, documentation, and qualification

- Finish grouped project cards, overview recipe status, new-data confirmation,
  exception summary, and history presentation.
- Add source-to-source added/changed/unchanged/absent summaries only if they can
  be derived set-wise from governed business keys without weakening evidence
  boundaries. This summary is informational and never a deletion policy.
- Update user workflow, developer workflow, evidence lifecycle, architecture
  map, glossary, and `docs/workflow.yml` coverage.
- Capture fictional-data screenshots and run visual/accessibility acceptance.
- Measure project-list and recipe-application query counts and timings.
- Run the focused and complete test suites in a writable Windows temp context.

**Gate:** a data-informed non-technical operator can complete the two-version
customer scenario with one obvious next action at each step and no manual
recreation of compatible rules.

## 13. Acceptance scenarios

### 13.1 Compatible replacement

Given a submitted Customer Recipe v1 and a complete later file with the same
logical structure, when the operator starts Data version 2, then all recipe
rules rebind, no old evidence becomes current, and the operator proceeds
without manual remapping.

### 13.2 Added and changed customers

Given stable governed customer business keys, new keys become creates, changed
approved fields become updates, and identical records become unchanged in a
fresh Odoo comparison. A changed business key appears as an absent old key and
new key; it is not guessed as an update.

### 13.3 Missing prior customer

A customer absent from the complete replacement file is reported only in an
informational cross-version summary when available. No delete/archive action
is inferred or authorized.

### 13.4 New selection value

`German` under explicit language coverage produces one blocking unmatched
value issue. Mapping it to an available current code clears the issue. An
unavailable code remains blocked after refresh.

### 13.5 New many2one value

`LUX` under explicit country matching requires confirmation to the portable
`LU` key. Missing and ambiguous target keys fail closed. No numeric Odoo ID is
stored.

### 13.6 Existing target value removed

A recipe target selection code removed from the current Odoo metadata produces
the existing selection-value-invalid issue and cannot be submitted.

### 13.7 Column reorder and rename

A uniquely named reordered column rebinds. A renamed used column blocks and
requires an explicit new binding. A new unused column is informational.

### 13.8 Derived and related datasets

Derived categories and relationships are recreated from the new physical
selection. They do not reuse prior derived rows or dataset IDs. Missing or
ambiguous references block preparation.

### 13.9 Concurrency and interruption

Two stale **Use new files** submissions cannot allocate the same edition or
both become current. Process interruption at every creation step leaves either
the old edition current or one recoverable, fully linked new edition.

### 13.10 Authorization and isolation

An actor cannot read or apply a recipe from an unrelated series. Recipe JSON,
credentials, file paths, and protected evidence do not leak through cards,
errors, logs, or URLs.

### 13.11 Performance and N+1

Listing 100 series with multiple editions executes a bounded registry query
set and opens no edition databases. Applying a recipe performs no Odoo call per
source row or distinct value and no parent scan per child relationship.

## 14. Expected code and documentation boundaries

Likely new modules:

- `src/impodo/domain/recipes.py` or a focused `domain/recipes/` package;
- `src/impodo/application/recipe_service.py`;
- `src/impodo/application/project_edition_service.py`;
- `src/impodo/adapters/duckdb/recipe_repository.py`;
- `src/impodo/adapters/duckdb/project_series_repository.py`; and
- `src/impodo/web/routers/recipes.py` if project routes would otherwise become
  too broad.

Likely existing modules to change:

- `src/impodo/projects.py` for safe project setup copying or a narrow new port;
- `src/impodo/access.py` only if existing capabilities cannot express the
  series operation without broadening authority;
- `src/impodo/adapters/duckdb/schema/registry.py` and registry queries;
- `src/impodo/adapters/duckdb/project_repository.py` for series projection and
  restart-safe linkage;
- `src/impodo/domain/mapping/contracts.py` for categorical coverage policy and
  the next strict contract version;
- `src/impodo/application/mapping_workspace_service.py` for recipe publication
  and value-coverage validation orchestration;
- derived-entity conversion services for logical rebinds;
- `src/impodo/web/app.py`, context, project/mapping presenters, routers, and
  templates;
- `src/impodo/web/target_credentials.py` and `src/impodo/secrets.py` for a
  narrow explicit same-target credential-copy operation; and
- navigation/project-list queries so internal editions do not become duplicate
  top-level projects.

Focused tests should include:

- a new `tests/test_recipes.py` and repository tests;
- `tests/test_projects.py`;
- `tests/test_workspace.py`;
- `tests/test_mapping_validation.py`;
- `tests/test_mapping_forms.py`;
- `tests/test_derived_entities.py`;
- `tests/test_readiness.py`;
- `tests/test_web_app.py`;
- `tests/test_project_security.py`;
- `tests/test_documentation_quality.py`;
- `tests/test_code_documentation.py`; and
- `tests/test_internal_release.py`.

The final implementation should run focused tests after each slice, then the
complete suite, `scripts/documentation_quality.py --check`, `git diff --check`,
and `git status --short`. Windows test runs must use a verified writable temp
directory before application failures are diagnosed.

## 15. Approaches explicitly rejected

### Replace the frozen files in place

Rejected because it breaks source hashes, snapshots, mapping bindings, audit
meaning, and downstream evidence.

### Copy the prior project DuckDB and delete rows from it

Rejected because it begins with stale evidence and relies on a fragile
denylist of tables/pointers to clear.

### Add a data-version column to every existing project table first

Rejected for the initial delivery because it expands a focused feature into a
cross-product schema and query rewrite. It can be reconsidered only if measured
operational needs make separate contained workspaces untenable.

### Copy the raw `MappingDefinition`

Rejected because it contains the old source-selection/schema hashes and
physical dataset/column identities.

### Match renamed fields or values with fuzzy/AI guesses

Rejected as an authoritative action. Impodo may show a bounded recommendation,
but ambiguous or inexact changes require explicit confirmation.

### Treat every many2one value as an enumerated choice

Rejected because high-cardinality relationships such as customers, products,
or accounts should resolve through governed keys in batches. Explicit choice
coverage is for bounded categorical domains such as country aliases.

### Reuse approvals, comparisons, or execution snapshots

Rejected because every one of those artifacts binds exact source, mapping,
schema, target, or prepared evidence.

### Infer deletes from missing rows

Rejected because a replacement export may be filtered or incomplete. Deletion
requires a separate explicit business policy, evidence model, and Odoo write
scope.

## 16. Definition of done

The feature is complete only when:

- one visible project can contain at least two immutable data versions;
- a submitted mapping publishes a stored, versioned, hash-verified recipe;
- a later complete file set can reuse all compatible business rules without
  copying old evidence;
- new Language selection and Country many2one choices produce focused,
  actionable blockers and can create Recipe v2 after confirmation;
- structural, schema, selection, relationship, and business-key drift fail
  closed;
- the browser keeps compatible rules green and offers one obvious next action;
- previous versions remain reopenable and cannot satisfy current readiness;
- registry listing and relationship matching have no N+1 project/row behavior;
- no recipe operation writes Odoo or stores a numeric Odoo ID;
- permanent series deletion cleans every contained edition and secret through
  governed services;
- focused, full-suite, documentation, visual, accessibility, and Windows
  qualification evidence passes; and
- current user/developer documentation is updated only after the behavior is
  implemented and verified.
