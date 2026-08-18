# Reusable recipes and data versions implementation plan

## Status and authority

**Status:** Active implementation plan from 2026-08-18. No phase is complete
unless the repository, tests, and current documentation say so.

**Phase 0:** Completed on 2026-08-18.

**Phase 1:** Completed on 2026-08-18 after the bounded implementation and
regression gate recorded below. Recipe publication, series, and edition
behavior remain unavailable.

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

An operator who has completed and submitted customer mapping authoring, whether
or not they also ran a rehearsal, can return two weeks later, choose **Use new
files**, upload the updated export, and retain the confirmed business rules
without recreating the project from scratch.

Impodo must:

1. keep the earlier files, rules, preparation, review, and load evidence
   immutable and reopenable;
2. create a fresh contained workspace for the new data version;
3. pin the then-current recipe when creating the pending edition and apply that
   exact revision only after the new source and current Odoo schema are known;
4. bind recipe datasets and columns to the new physical dataset identities;
5. reuse transformations, business keys, value matches, relationship rules,
   source-preparation rules, reusable quality rules, reference dependencies,
   validations, and control definitions when compatible;
6. surface new or stale values, columns, target fields, and relationships as
   targeted recovery actions;
7. prevent mapping submission, preparation, or loading while a required
   exception remains unresolved; and
8. create a new immutable recipe revision when the corrected mapping is
   confirmed.

Reusable configuration is not reusable current evidence. In particular,
normalization corrections, approvals, expected control-total values, source or
target snapshots, validation results, comparisons, execution snapshots, and
write journals remain specific to one data version. An operator who wants a
normalization correction to recur must promote it into a mapping
transformation, value match, governed reference dependency, or reusable quality
rule before it can enter a recipe.

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
- preparation also consumes a current source-preparation plan, quality ruleset,
  approved coverage, and reference bundle. A recipe based only on
  `MappingDefinition` would silently omit reusable business configuration.
- formula expressions may refer to physical `column_<ordinal>` aliases, and
  reference lookups contain exact project-local reference IDs and hashes. Those
  constructs need explicit logical conversion or must make Recipe v1
  ineligible.
- source `candidate_type` is inspection guidance rather than a guaranteed
  runtime type, so a changed inference is not automatically an incompatible
  physical contract.
- the central `registry.duckdb` stores lightweight project summaries but has no
  project-series, data-version, or recipe lineage.

The feature must not replace files in a frozen project, mutate old mapping
JSON, copy a project database, or edit project storage directly. It must create
new evidence and explicitly rebind portable meaning.

## 3. Product vocabulary

### 3.1 Project series

The business migration scope visible to the operator, for example **Customer
migration**. In code and technical contracts this is a distinct aggregate such
as `ProjectSeries`; it owns data-version history and the current recipe. The
browser may continue to label it **Project**, but a `series_id` is never a
`project_id` and is never accepted by project-scoped authorization, credential,
filesystem, or deletion operations.

### 3.2 Migration project or edition workspace

The existing `MigrationProject`, identified by `project_id`, remains one
contained governance and evidence boundary with its own DuckDB and artifacts.
Each workspace is one edition in exactly one project series. Existing domain
contracts must not redefine `MigrationProject` to mean the series.

### 3.3 Data version

The operator-facing name for one edition workspace and its source package.
Examples are **Data version 1 - rehearsal** and **Data version 2 - 31 August
export**. Evidence written within an edition remains immutable and versioned,
but an unfinished edition may still advance its normal current pointers until
the edition is sealed.

Each data version uses a distinct `project_id` and contained DuckDB workspace
internally. This preserves the existing singleton and invalidation contracts
while the browser groups those workspaces under one visible project.

### 3.4 Recipe

Portable, versioned business meaning that can be rebound to another compatible
data version. A recipe revision is append-only and content-hashed. It is not a
source snapshot, target snapshot, preparation result, approval, or permission
to write Odoo.

### 3.5 Recipe application

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

### 4.2 Store recipe metadata and payloads through separate boundaries

Recipe revisions belong to the project series, not to one edition. Extend the
registry with bounded recipe metadata, hashes, current pointers, and protected
storage keys. Store the canonical payload in a series-scoped `RecipeStore`,
protected artifact boundary, or dedicated series database rather than casually
placing every series' business literals in the lightweight global registry.
The payload may contain confidential aliases, internal codes, reference values,
or constants, so it inherits the series classification, retention, access,
backup, and deletion policy and must never be exposed across a series.

Do not store source rows, distinct-value scans, credentials, Odoo numeric IDs,
protected provenance, or row-level compatibility issues in the registry.

### 4.3 Make edition state and series invariants explicit

`ProjectSeries` owns a UUID generated independently from every child
`project_id`, its optimistic revision, one current registered edition, and at
most one pending edition. Existing projects receive a newly generated series
UUID during backfill; `series_id = project_id` is forbidden because identical
identifier values make scope-confusion bugs more likely.

The first release treats the business purpose, source mode, source system,
non-secret Odoo connection target, intended applications/models,
classification, and retention policy as series-owned invariants. Starting a new
edition copies those values through a field-level allowlist and prevents them
from diverging.
A materially different target or purpose requires a new series. Edition-owned
values such as export status, export date, lifecycle label, files, and current
workflow evidence are always new.

Edition metadata uses explicit states:

- `PENDING`: a resumable child workspace exists but is not the current
  registered edition;
- `ACTIVE`: the one current registered edition that may accept workflow
  mutations;
- `SEALED`: a historical edition that is reopenable but rejects every mutation;
- `ABANDONED`: an unfinished edition discarded through governed cleanup.

The series state is `ACTIVE` or `DELETING`; `DELETING` is the tombstone while a
series-deletion intent is completed or recovered.

Creating a pending edition does not displace the current active edition. The
pending edition becomes active and the prior edition becomes sealed only after
the new workspace is registered with its new export identity. A pending edition
may be resumed, or discarded only while it has no frozen source selection,
active job, or downstream immutable workflow evidence. Sealing must be enforced
below the browser through a series-aware application policy and a project-local
seal marker; merely hiding controls or relying on a registry pointer is not a
sufficient immutability boundary.

### 4.4 Keep application results project-local

The successful application materializes a normal `MappingWorkingDraft` in the
new edition, bound to its current source-selection and schema hashes. The
application audit event records recipe ID, version, semantic hash, and payload
hash.

Structural compatibility issues are calculated from the immutable recipe,
source snapshots, schema snapshot, and a narrow project-local
`RecipeApplicationDraft`. That draft stores only explicit dataset/column
binding overrides, its optimistic revision, dependency hashes, and current
issue fingerprints. It cannot represent mapping semantics or authorize
preparation. Once all structural bindings resolve, it is consumed to create the
normal `MappingWorkingDraft`. Categorical coverage issues then join mapping
validation through separately hash-bound coverage evidence.

## 5. Recipe contract

Introduce a strict `RecipeDefinition` contract independent of
`MappingDefinition`. It must exclude instance-specific IDs and hashes while
retaining enough information to create a new exact mapping.

### 5.1 Compose the reusable business configuration

Do not flatten every current runtime object into one mapping-shaped payload.
`RecipeDefinition` is a composition of strict subcontracts:

- `SourceShapeRecipe`: logical datasets and columns plus compatibility
  expectations;
- `SourcePreparationRecipe`: portable derived-entity, parent/child, exact join,
  union, grouping, and aggregate definitions supported by Recipe v1;
- `MappingRecipe`: target models and modes, scalar providers,
  transformations, validations, relationships, dispositions, approved write
  fields, comparison policy, value matches, and unmatched-value policies;
- `TargetGovernanceRecipe`: business keys, scope, and exact semantic target
  dependencies;
- `QualityRecipe`: portable manager-authored rules and reusable approved-scope
  applicability, while mapping/schema-derived rules are regenerated;
- `ReferenceDependencies`: logical, content-hashed governed reference packages
  needed by mapping or quality rules; and
- `ControlDefinitions`: what must reconcile, excluding edition-specific
  expected values by default.

Every node receives a deterministic logical ID derived from its semantic path
or another canonical name. Random mapping, plan, rule, project, reference, and
dataset IDs never leak into recipe semantics.

The recipe is execution-engine neutral: it contains no DuckDB concepts,
Parquet paths, PostgreSQL SQL, or engine-selection decisions. The edition
workspace decides how to execute the portable meaning.

### 5.2 Semantic and payload hashes

Each revision has two hashes:

- `semantic_hash` covers the canonical reusable business configuration and is
  the identity used to decide whether a new recipe version is needed; and
- `payload_hash` covers the complete stored canonical bytes, including bounded
  compatibility hints and contract metadata, and is verified whenever the
  artifact is read.

Both include the applicable recipe contract versions. Both exclude author,
timestamp, origin project, source-selection/schema hashes, and mapping/plan
revision identities. Prior file display names, exact ordered header signatures,
and ordinals are diagnostic binding hints rather than business meaning and do
not enter the semantic hash. Confirming unchanged meaning reuses the current
recipe revision; it does not append a new version merely because a file ID,
column order, or unrelated schema field changed.

### 5.3 Logical dataset and column bindings

A physical recipe dataset stores:

- the operator-confirmed dataset name;
- expected source kind (`FILE` in the first release);
- selected-table hints such as prior display name, header row, and exact header
  signature;
- logical recipe-column IDs;
- exact prior source names;
- prior ordinals for diagnostics only;
- expected candidate types; and
- whether each column is required by identity, mapping, relationship,
  derivation, formula, validation, quality, reference, or control rules.

Recipe rules refer to logical recipe dataset/column IDs. They never refer to a
registered `file_id`, physical `dataset_id`, table key, source hash, snapshot
path, or current column stable key.

Application matching is deliberately conservative:

1. match datasets by the operator-confirmed dataset name;
2. match columns by exact source name when the name is unique;
3. permit reordered columns after exact unique-name binding and successful
   semantic compatibility checks;
4. never accept duplicate source headers, which the current source-confirmation
   boundary already rejects;
5. never fuzzy-match renamed columns without confirmation; and
6. report new unused columns or datasets as information rather than silently
   mapping them.

`candidate_type` drift alone is a review signal, not a blocker. Application
blocks only when the current values and declared provider/transformation/type
requirements demonstrate an incompatible semantic change. Formula policies
that use `column_<ordinal>` must be parsed and rewritten through logical column
IDs before reorder compatibility can be claimed; otherwise the recipe is
ineligible.

### 5.4 Target dependency bindings

Recipes retain Odoo technical model and field names, field roles, business-key
order, scope, and portable selection/business-key values. They do not retain
numeric record IDs or claim that the prior schema remains current.

Store a canonical `RecipeTargetDependencyFingerprint` for only the models,
fields, selections, relation targets, business-key fields, scope fields, and
required-field coverage on which the recipe depends. Application resolves those
dependencies against a newly captured Odoo 19 schema. An unchanged dependency
may be rebound even when the complete schema hash changed elsewhere.

Compatibility is direction-sensitive. A dependency blocks when it becomes
missing, readonly for an intended write, type-incompatible, relation-incompatible,
or newly required without a provider/disposition. A harmless relaxation or an
unrelated schema change does not block. A successful application still creates
a new `MappingDefinition` bound to the complete current schema/governance hash.

### 5.5 Recipe v1 eligibility

Eligibility is an application-service gate, not merely a hidden browser action.
Recipe v1 supports only:

- registered `FILE`-origin editions in the same series-owned target and purpose;
- the new strict mapping contract containing categorical coverage and split
  control-definition/expectation semantics;
- mapping modes `UPSERT`, `CREATE`, and `REFERENCE`; and
- source-preparation, quality, formula, and reference constructs for which a
  reviewed logical converter and round-trip acceptance fixture exists.

Recipe v1 rejects `ODOO_PINNED_UPDATE`, Odoo-origin sources, numeric target IDs,
unconverted ordinal formulas, project-local reference IDs, unsupported
source-preparation rules, stale/incomplete submissions, and any current quality
or coverage dependency that would otherwise be lost. Rejection returns one
bounded eligibility report listing the exact unsupported constructs and their
recovery actions.

Approved reference data that is supported for reuse is republished as a
series-scoped, content-addressed protected recipe dependency with logical IDs.
Application materializes a fresh edition-local reference bundle with new local
IDs and verifies the exact dependency hash. It never treats the prior
project-local `reference_id` as portable.

### 5.6 Categorical coverage policy and evidence

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
policy. Implement this as mapping contract version 11 with strict parsing; do
not reinterpret stored version 8-10 JSON in place.

The mapping-contract upgrade must precede recipe extraction. Lazy bootstrap
from versions 8-10 opens a focused review for every affected field and creates a
new checked and submitted mapping before Recipe v1 can be published. Impodo
must not infer whether an old partial value match meant aliases or a closed
categorical domain.

Introduce immutable `CategoricalCoverageEvidence` bound to the mapping hash,
effective source-selection hash, exact source snapshot hashes, scan contract,
normalization semantics, and the target-choice or target-reference evidence it
used. Scan each dataset once for all relevant fields. `EXPLICIT_*` policies use
the same trimmed raw-choice semantics as runtime value matching;
`EXACT_TARGET_VALUE` evaluates the declared transformation semantics. A formula
or multi-column dependency must use a set-based supported evaluator or make the
recipe ineligible; scanning one raw column is not sufficient.

If missing or ambiguous relationship targets must block mapping submission,
the evidence also binds current target-reference snapshot hashes and read
principal, permission, context, and connection identity. Those target hashes
participate in validation and cache keys because target records can change
without a schema change. Otherwise the product must explicitly defer the
condition to preparation and must not claim it blocks mapping submission.

### 5.7 Reusable control definitions and edition expectations

`BusinessControlTotal.expected_total` is edition-specific unless an explicit
business contract says it is invariant. Recipes therefore reuse the control
name, target field, unit, tolerance, and calculation method, but application
creates a fresh `EditionControlExpectation` supplied or confirmed for the new
data version. The old expected number remains origin evidence and is never
silently copied into the new mapping.

The new mapping contract must separate control definitions from exact
expectations while continuing to parse stored versions 8-10 unchanged. Recipe
application cannot produce a submittable mapping until every required new
expectation is present. The same contract distinguishes reusable semantic
constants from explicitly declared edition parameters such as extract or batch
dates.

### 5.8 Recipe provenance

Each immutable revision records outside its semantic hash:

- `recipe_id`, version, and parent version;
- series ID;
- semantic hash, payload hash, byte length, storage key, and contract versions;
- originating project/data-version ID;
- originating mapping ID, version, and content hash;
- originating effective source-selection/snapshot hashes and target
  schema/governance hashes;
- originating source-preparation plan, governance, quality-ruleset, approved
  coverage, and reference-bundle hashes where applicable;
- actor issuer, stable subject, display name, and timestamp; and
- a bounded reason such as `MAPPING_SUBMITTED` or `EXISTING_PROJECT_BOOTSTRAP`.

Only a successfully submitted, current mapping may publish a reusable recipe
in the first release. A stale or unsubmitted working draft cannot become the
series recipe. Provenance distinguishes the actor who originally submitted the
mapping from the actor who later performed a lazy recipe bootstrap. Neither
identity is inferred from a display name alone.

## 6. Registry and persistence changes

Add an additive, transactionally versioned registry migration. Keep series
metadata and recipe revision metadata in the registry, but permit the minimum
additive project-database changes needed to enforce edition sealing and persist
an application draft. Do not duplicate recipe payloads across project databases
or add a data-version discriminator to every existing table.

### 6.1 Proposed registry tables

`project_series`

- independent `series_id` UUID primary key, never accepted where a project ID is
  required;
- display name and lifecycle state, including a deletion tombstone;
- current registered/active `project_id`;
- optional pending `project_id`;
- series-owned business purpose, source mode/system, non-secret target
  connection settings, intended applications/models, classification, and
  retention policy;
- setup-hydration state and hash for legacy series;
- optimistic revision;
- created/updated timestamps.

`project_edition`

- `project_id` primary key;
- `series_id`;
- positive edition number unique within the series;
- optional parent `project_id`;
- lifecycle state: `PENDING`, `ACTIVE`, `SEALED`, or `ABANDONED`;
- recipe ID/version applied, nullable until application;
- created timestamp and actor identity; and
- lifecycle label supplied by the operator;
- new export/as-of date and intake status; and
- bounded history projections: workflow status, source row summary, seal hash,
  sealed timestamp/actor, and registry synchronization revision.

`recipe_revision`

- composite primary key `(recipe_id, version)`;
- `series_id` and optional parent version;
- unique semantic hash within the series;
- full payload-integrity hash, byte length, and protected storage key;
- constituent contract versions;
- origin identifiers and hashes;
- actor identity and timestamp.

The confidential recipe document itself is stored in a protected,
series-scoped `RecipeStore` or dedicated database, not as casually queryable JSON
in the global registry. The store validates the full payload hash before parse
and the semantic hash after canonicalization.

`recipe_current`

- `series_id` primary key;
- current recipe ID/version; and
- current semantic and payload hashes.

`recipe_publication_intent`

- operation ID, series ID, source project ID, and mapping submission hash;
- expected series revision and intended parent/current recipe;
- state, retry count, correlation ID, bounded error category, and timestamps.

Mapping submission and recipe storage span project and registry stores. A
successful mapping remains submitted if recipe publication is interrupted; the
intent/outbox is retried idempotently, and **Use new files** remains unavailable
until the recipe revision and current pointer are complete.

`project_edition_creation_intent`

- operation ID and series ID;
- reserved project ID and edition number;
- parent project ID;
- state and timestamps.

The creation intent closes the cross-database/filesystem seam. Startup recovery
must finalize an existing contained workspace or remove only an empty,
unpublished workspace created by that exact intent. It must never guess at or
delete an unrelated project directory.

`credential_copy_intent`

- operation ID, series ID, source and destination project IDs;
- exact connection-target hash and `READ` role;
- source secret generation, destination generation, state, and timestamps.

This intent makes an explicit credential-copy operation idempotent without
placing secret material in either database.

`series_deletion_intent`

- operation ID, series ID, tombstone revision, actor, and timestamps;
- the exact edition, recipe, credential, key, job, artifact, and directory
  targets enumerated before deletion; and
- per-target progress plus a bounded error category.

Deletion first tombstones the series so no new edition or recipe can be created.
It then retries only the enumerated targets through their governed deletion
services. Recovery must never infer targets by scanning unrelated directories.

`registry_schema_migration`

- monotonically increasing migration version;
- migration name, checksum, start/completion timestamps, and outcome.

Each schema change runs transactionally where supported and has an idempotent
recovery path where filesystem work prevents one transaction.

### 6.2 Existing-project migration

On startup, apply the migration ledger and backfill each unlinked
`project_registry` row in one idempotent transaction as:

- a newly generated `series_id` distinct from the project ID;
- edition number `1`;
- no parent edition;
- state `ACTIVE`, with current registered project pointing to the existing
  project;
- setup hydration `PENDING`, because the current lightweight registry contains
  only project ID, name, status, revision, and update time; and
- no current recipe until one is safely bootstrapped.

Backfill must be idempotent and must not open every project database during the
normal project-list query. Recipe bootstrap is lazy: an eligible existing
project first authorizes and opens only its current workspace, copies the
series-owned allowlist into the registry, validates it, records the setup hash,
and marks hydration `READY`. It can then create Recipe v1 from its current
submitted mapping when the user chooses **Use new files** or explicitly confirms
**Create reusable recipe**. The action is unavailable while hydration is
incomplete. If its mapping contract predates the strict recipe-eligible
contract, the focused upgrade/review described in Phase 1 is required before
bootstrap.

If the project has no current submitted mapping, the browser directs the user
to finish and confirm the mapping. It must not silently use a working draft.

### 6.3 Repository concurrency

- Use optimistic series revision checks for edition reservation, pending and
  current-registered pointers, publication, activation, and deletion.
- Serialize recipe version allocation inside one registry transaction.
- Reuse an identical current recipe by semantic hash after verifying its full
  payload hash.
- Reject a supplied parent recipe that is no longer current and ask the user
  to reload.
- Keep project registry synchronization and edition linkage restart-safe through
  explicit intents/outboxes; do not claim cross-store atomicity.
- A pending edition does not replace the current registered edition. Activation
  occurs only after its workspace is registered and the prior active edition is
  sealed. Resume or discard addresses the exact pending ID.
- List project series, their current editions, pending state, and history
  summaries from registry read models in bounded set-based queries; do not open
  one DuckDB database per card or history row. Every child state change that
  affects a projection has a versioned synchronization event and recovery path.

### 6.4 Authorization scope

Existing project-scoped authorization is insufficient for operations that
cross editions. Add explicit series-scoped capabilities for series view, edition
create/resume/discard, recipe publish/apply, credential copy, and series delete.
An operation must also authorize every source or destination project it opens;
membership in a series is not an authority shortcut. Routes reject a series UUID
where a project UUID is expected and vice versa.

## 7. New data-version lifecycle

### 7.1 Preconditions

Show **Use new files** only when:

- the series has one current registered edition and no unresolved pending
  edition; an existing pending edition instead offers **Resume** or **Discard**;
- the source mode is `FILE`;
- a current submitted, Recipe-v1-eligible mapping exists and any legacy
  categorical-policy upgrade has been reviewed;
- that mapping still matches the current source selection and schema;
- no preparation or execution job is active for the current edition; and
- the actor has series view and edition-create capabilities plus source-project
  view and the new project's required setup, registration, source, schema,
  mapping, and governance capabilities.

Recipe publication requires both existing mapping-submission authority on the
origin project and recipe-publish authority on the series. Recipe application
requires recipe-apply authority on the series and mapping-edit authority on the
new edition. Credential copy is a separate capability. The first release
exposes no cross-series recipe catalogue.

The first release uses the current submitted mapping as the publication source;
a completed execution rehearsal is not a hidden prerequisite. If product policy
later requires a successful rehearsal, add the exact eligible terminal status
to this contract and its acceptance fixtures before implementation.

### 7.2 Start the edition

The confirmation page states:

- previous files and outcomes remain unchanged;
- the new upload is treated as a complete replacement snapshot;
- mapping and transformation rules will be reused where compatible;
- missing old records are not deletion instructions; and
- all preparation, Odoo comparison, and load evidence will be recreated.

It also collects or confirms the new export/as-of date. That date and the
edition label are edition parameters, not reusable recipe constants.

After confirmation, `ProjectEditionService`:

1. reserves the next edition through a creation intent;
2. creates a clean project workspace through the normal repository boundary;
3. applies the series-owned business purpose, source mode/system, non-secret
   target settings, intended applications/models, classification, and retention
   policy as locked setup values;
4. resets export date/status to the newly confirmed edition values;
5. links the workspace as `PENDING` to the series and parent edition without
   changing the current registered pointer; and
6. records actor-bound `DATA_VERSION_CREATED` events in the registry and new
   project audit.

The operator can resume the exact pending workspace or discard it while it is
still empty/unregistered. After required setup and files exist, normal project
registration completes through the creation intent: seal the prior active
edition, register and activate the new one, then advance the current registered
pointer. A failure leaves the prior edition active and the new edition pending
and recoverable; it cannot strand a half-created edition as current.

It must not copy source files, catalogues, selections, snapshots, derived
results, mapping tables, schema snapshots, target record snapshots,
preparation, quality, normalization, comparisons, approvals, protected
evidence, execution snapshots, or journals.

### 7.3 Credentials

Credentials are not recipe content. Copying non-secret connection settings
does not authorize reuse of a credential.

When the prior and new connection-target hashes are identical, the browser may
offer **Reuse saved read connection** as a separate explicit confirmation after
checking `CREDENTIAL_COPY` authority. A narrow, intent-backed secret-store
operation may copy only a persistent `READ` credential into a new
project-specific secret generation for the exact target. It never returns the
secret to the browser and records a non-secret central/series event plus a new
edition audit event. It must not mutate the sealed prior edition merely to add
an audit row. Session-only credentials that are no longer present cannot be
reused.

Immediately re-probe the copied credential and bind the new probe evidence,
principal, permission set, context, connection identity, and secret generation
to the new edition. A failed probe leaves the new edition without a usable read
credential. Never copy a `WRITE` credential during edition creation; the
operator supplies or explicitly establishes write authority later at the normal
load boundary.

If the target settings differ, credential reuse is unavailable. Target changes
continue to invalidate bindings through the existing project service.

### 7.4 Upload and freeze

The operator uploads the complete replacement source set through the existing
intake boundary while the edition is pending, then registers it through the
normal boundary before source inspection/freeze. Impodo inspects each new file
and presents the expected logical datasets from the recipe as suggestions.

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

1. load and verify the exact recipe revision pinned when the pending edition was
   reserved; if it is no longer current, require an explicit restart/rebase;
2. bind logical datasets and columns to the new effective source selection;
3. create/update a project-local `RecipeApplicationDraft` containing binding
   overrides, dependency hashes, issues, and completion state only;
4. materialize supported source-preparation rules and rebuild derived rules
   against new dataset/column IDs;
5. materialize fresh edition-local reference bundles, reusable quality rules,
   approved coverage policy, governance, and control definitions;
6. resolve target models, fields, keys, scope, types, and relation metadata
   against the current schema;
7. collect/confirm every required `EditionControlExpectation` and edition
   parameter;
8. construct a fresh `MappingDefinition` with a new mapping ID and the new
   source-selection/schema hashes;
9. validate its semantics through the existing mapping validator;
10. scan each affected dataset once for all required categorical source fields
    through a bounded, set-based local snapshot service;
11. bind immutable categorical coverage evidence, including current target
    reference evidence where the policy requires it;
12. persist the mapping working draft only when structural binding is complete;
    and
13. record `RECIPE_APPLIED` with recipe, application-draft, and resulting
    mapping-draft hashes.

User-confirmed renamed-column or other rebindings live in
`RecipeApplicationDraft`, so a reload or target refresh does not lose them. They
do not mutate the recipe or become reusable semantics until a corrected mapping
is submitted and a later recipe revision is published. Likewise, observed
normalization corrections remain edition evidence unless explicitly promoted to
a supported reusable preparation or mapping rule.

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
| Several unconfirmed file tables are plausible for a logical dataset | `RECIPE_DATASET_BINDING_REQUIRED` | Needs review | Confirm one table during source selection |
| New dataset not used by the recipe | `RECIPE_DATASET_NEW` | Information | Ignore it or extend the mapping deliberately |
| Used source column absent or renamed | `RECIPE_SOURCE_COLUMN_MISSING` | Blocker | Bind the new column or correct the source |
| Duplicate source headers prevent confirmation | Existing source-confirmation issue | Blocker | Correct the file headers; recipe ordinal hints do not disambiguate duplicates |
| Candidate source type changed | `RECIPE_SOURCE_TYPE_REVIEW` | Needs review by default | Confirm parsing/transformation; block only when semantic validation proves incompatibility |
| New unused source column appears | `RECIPE_SOURCE_COLUMN_NEW` | Information | Ignore or map it deliberately |
| Derived rule cannot bind an input | `RECIPE_DERIVED_RULE_UNBOUND` | Blocker | Repair the source binding before deriving rows |
| Ordinal formula cannot be rewritten to logical columns | `RECIPE_FORMULA_NOT_PORTABLE` | Blocker | Rewrite it through the supported logical formula contract |
| Required reference bundle is absent or changed incompatibly | `RECIPE_REFERENCE_STALE` | Blocker | Republish or bind the governed reference dependency |
| Required edition control expectation is missing | `RECIPE_CONTROL_EXPECTATION_REQUIRED` | Blocker | Enter or confirm the new edition's expected value |
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
  or cleans the exact creation intent; an initialized pending edition is resumed
  or explicitly discarded, not guessed away.
- An interrupted activation leaves the prior edition current until recovery can
  verify the new edition is registered and the prior edition is sealed.
- An interrupted recipe publication leaves the mapping submitted and the
  idempotent publication intent pending; it never points at a missing payload.
- A recipe parse/hash failure blocks use and preserves the stored bytes for
  diagnosis.
- A source/schema change during application aborts before saving the mapping
  draft while retaining safe binding overrides in `RecipeApplicationDraft`.
- A failed categorical scan returns a retryable local-read error without
  changing recipe or mapping pointers.
- Odoo connection failures retain the new source evidence but block schema
  refresh and recipe application.
- A credential-copy or re-probe failure leaves the new edition without an
  accepted read credential and does not expose or reuse write authority.
- Interrupted series deletion retains its tombstone and exact deletion intent;
  retry continues only the enumerated remaining targets.
- Unknown Odoo write outcomes remain governed by the existing execution
  journal; recipe reuse never authorizes a blind retry.

## 9. Browser experience

### 9.1 Project list and overview

The project list shows one card per series, not one card per internal edition.
The card includes the current data-version label and recipe version without
opening child databases. If a pending edition exists, it is shown separately as
**Setup in progress** with **Resume** and, while policy allows, **Discard**; it
does not replace the current data label.

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
wizard. Prefill an editable data-version label and export/as-of date, and show
exactly what is kept and recreated. The page separately confirms optional saved
read-credential reuse and states that write access will not be reused.

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
- export/as-of date and received/created timestamp;
- lifecycle state;
- recipe version applied;
- workflow status;
- source row summary when available; and
- a read-only link to the edition's evidence.

Historic editions never become current merely because they are opened. Every
mutating application service rejects a sealed edition even if a route, API
client, background job, or stale browser request bypasses the read-only UI. A
project-local seal marker provides the contained database with the same policy;
browser disabling alone is not an enforcement boundary.

### 9.5 Deletion and retention

For the first release, the normal project delete action operates on the whole
series and explicitly names the number of data versions and recipe revisions
that will be permanently deleted. It must enumerate and remove each edition's
credentials, keys, jobs, artifacts, project directory, registry row, series
metadata, and protected recipe history through existing governed deletion
services. The confirmation creates the tombstoned `series_deletion_intent`
before removing anything. Partial failure is visible and safely retryable; it
does not resurrect the series or silently report success.

Independent historical-edition purge is deferred until recipe provenance,
retention, current-pointer recovery, and credential cleanup have a complete
contract. Do not expose a partial delete button first.

## 10. Security, governance, and Odoo 19 rules

- Recipes contain no credentials, session tokens, local file paths, source rows,
  or numeric Odoo record IDs. Provenance may retain exact immutable artifact
  hashes outside recipe semantics.
- Formula and pattern rules retain the existing allowlists and bounds. Recipe
  application never evaluates arbitrary code.
- Recipe reads and writes are series-scoped and actor-authorized, and project
  access is checked independently for each edition opened.
- Recipe payloads inherit project classification and local filesystem
  protections through the series-owned classification/retention policy and live
  in the protected RecipeStore. Browser responses include only the selected
  series and never expose the full payload unless a specific authorized workflow
  requires it.
- Every publication and application records stable actor identity.
- Historical sealing is enforced at application/repository boundaries, not only
  in browser presentation. Central events describe cross-edition operations so
  sealed project audit bytes remain unchanged.
- Only an explicitly confirmed, persistent read credential may be copied into a
  new generation and re-probed. Write credentials are never copied by edition
  creation.
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
- Project-series history uses bounded registry queries and no per-edition
  database opens.
- Recipe extraction walks mappings, rule graphs, reusable preparation, quality,
  coverage, references, governance, and controls once.
- Dataset/column compatibility uses indexed dictionaries keyed by logical name
  and exact source name.
- Distinct categorical values are computed by one set-based DuckDB/Polars scan
  per affected dataset for all required frozen columns.
- Target selection choices come from the captured schema snapshot.
- Many2one candidate keys are retrieved in bounded batches and indexed once
  per model/key/scope combination.
- Duplicate and missing relationship checks reuse run-scoped indexes during
  preparation.
- The existing value-mapping contract maximum remains enforced; raising it is
  separate performance work.
- Recipe bytes, logical nodes, datasets, fields, rules, reference literals,
  issues, distinct values, target candidate keys, and aggregate evidence are
  independently bounded before persistence or browser rendering.
- Repeated compatibility views may cache only hash-bound results keyed by
  recipe semantic and payload hashes, effective source-selection hash,
  application-draft binding hash, required target-schema dependency hash,
  categorical-evidence contract/hash, and any required target-reference
  snapshot/principal/permission/context/connection hashes. A whole-schema hash
  alone is too broad for reuse and insufficient for mutable target values; any
  required dependency change invalidates the cache.

Measure recipe extraction/application separately from full preparation. A
compatible recipe application should scale with datasets, columns, rules, and
distinct governed choices rather than total source rows, apart from the
columnar distinct-value scan.

Report reuse counts using stable definitions: a rule is reused only when its
semantic recipe node materializes unchanged; a rebound or operator-edited node
is reported separately. Do not count containers, validation passes, or inherited
labels as reused rules.

## 12. Implementation sequence

### Phase 0 - Freeze contracts and acceptance fixtures

- Record a decision that `ProjectSeries` is a new aggregate above the existing
  `MigrationProject`; it does not rename or redefine the current project domain.
- Correct the [architecture overview](../architecture/overview.md), which
  describes preparation as file-only: the current `PreparationService` also
  handles Odoo-origin sources.
- Reconcile this feature with [Impodo remaining work](remaining-work.md). If
  product ownership adopts it ahead of the currently stated unconditional
  related/mixed-source 100,000-row objective, update that authoritative roadmap
  in the same decision;
  this plan alone does not silently change priority.
- Add customer fixtures for Data versions 1 and 2 with added, changed,
  unchanged, and absent business keys.
- Add new `German` language and `LUX` country values.
- Add stale target-choice, missing country, ambiguous custom many2one,
  reordered ordinal-formula column, renamed-column, duplicate-header,
  candidate-type-drift, reference-bundle, quality-rule, and new-unused-column
  cases.
- Decide the exact composite recipe, categorical policy/evidence, control
  definition/expectation, application draft, lifecycle, intent/outbox, semantic
  hash, payload hash, and size/aggregate-bound contracts.
- Produce an explicit Recipe v1 eligibility matrix covering mapping constructs,
  formulas, references, preparation rules, quality rules, coverage policy,
  source mode, and target governance.
- Record baseline project-list, mapping submission, value-choice, preparation,
  and comparison behavior.

Phase 0 evidence:

- [ADR-012](../decisions/README.md#adr-012--project-series-group-contained-migration-projects)
  fixes the aggregate boundary;
- [the frozen proposed contracts](reusable-recipes-phase-0-contracts.md) define
  shapes, versions, eligibility, recovery, and bounds;
- [the deterministic acceptance fixture](../../fixtures/recipes/phase-0/acceptance-contract.json)
  and `tests/test_recipe_phase_zero_contract.py` cover the two-edition examples;
  and
- [the current behavior baseline](../reports/reusable-recipes-phase-0-baseline-2026-08-18.md)
  records repository ownership and focused verification.

**Gate:** domain examples serialize deterministically and every expected
recovery action is agreed before persistence or browser work; the roadmap either
adopts the priority explicitly or implementation does not begin.

**Gate status:** Passed. Contract, fixture, architecture-documentation, and
baseline checks pass, and the roadmap explicitly adopted Phase 1 on
2026-08-18.

### Phase 1 - Make mapping contract v11 recipe-safe

**Status:** Completed on 2026-08-18.

- Add mapping contract v11 with the strict categorical coverage enum and a
  separate reusable control-definition/edition-expectation shape.
- Implement immutable, bounded `CategoricalCoverageEvidence` over exact
  transformed semantics, scanning each dataset once for all relevant fields.
- Add current target-reference evidence when missing/ambiguous relationship
  targets are a mapping-submission blocker.
- Perform a focused v8-v10 upgrade review. Stored payloads still parse unchanged,
  but legacy inferred categorical behavior must be explicitly confirmed before
  a mapping becomes recipe-eligible.
- Extract browser-only source-choice enumeration into an application service and
  make evidence participate in validation hashes.

**Gate:** no eligible submitted mapping can silently accept a new categorical
source value; legacy mappings have deterministic reviewed/unsupported outcomes.

**Gate status:** Passed. Mapping contract v11, validation-bound categorical
evidence, split control semantics, the focused legacy review boundary, and the
shared application-layer source scan are implemented. Relationship target
existence/uniqueness remains explicitly deferred to fresh preparation evidence,
so mapping validation does not make a target-record coverage claim. See the
[Phase 1 implementation report](../reports/reusable-recipes-phase-1-mapping-contract-2026-08-18.md).

### Phase 2 - Add series, lifecycle, and protected persistence

- Add the registry migration ledger, independent-series-ID backfill, lifecycle
  projections, pending/current registered pointers, and project-local seal and
  recipe-application-draft storage.
- Implement `ProjectSeriesRepository`, protected `RecipeStore`, and recipe
  metadata repository with optimistic concurrency, both hash validations, and
  bounded payloads.
- Add immutable recipe revision/current-pointer contracts.
- Add publication, edition-creation, credential-copy, and series-deletion
  intents/outboxes with fault-injection recovery tests.
- Add series capabilities and explicit cross-scope authorization tests.
- Change project list and history queries to return set-based series projections.
- Preserve current single-edition routes through the current `project_id`.

**Gate:** all existing projects appear as one-edition series without opening
their contained databases in the list query; interrupted creation recovery is
deterministic; a series ID cannot be confused with its project ID.

### Phase 3 - Extract and publish composite recipes

- Implement deterministic conversion from the current source selection,
  reusable preparation, derived plan, submitted mapping, schema governance,
  quality rules, approved coverage, references, and control definitions to
  `RecipeDefinition`.
- Replace physical dataset/column/reference IDs with deterministic logical
  recipe bindings. Parse/rewrite supported ordinal formulas through those
  logical IDs and reject the rest.
- Reject numeric IDs, secrets, stale evidence, unsupported mapping contracts,
  Odoo-origin sources, pinned Odoo update, unsupported/local-only references,
  incomplete quality/coverage dependencies, and incomplete submissions.
- Republish supported reference data into the protected series recipe rather
  than retaining project-local IDs.
- Queue idempotent recipe publication from the successful mapping-submission
  workflow and expose pending/failed publication truthfully.
- Add lazy **Create reusable recipe** bootstrap for eligible existing projects.
- Record recipe provenance and audit.

**Gate:** two semantically identical mappings over different file IDs produce
the same semantic hash and valid payload hashes; changing one semantic
transformation, value match, quality rule, reference, or control definition
changes it, while changing only provenance does not.

### Phase 4 - Create, activate, seal, and delete data versions

- Implement `ProjectEditionService` and the new-data confirmation route.
- Apply series-owned setup to a fresh pending project workspace, collect a new
  export/as-of date, and reset edition status/evidence.
- Add resume/discard and activate only after normal registration; seal the prior
  edition through application/repository policy before switching current.
- Add explicit same-target persistent `READ` credential copy into a new
  generation followed by re-probe. Never copy `WRITE` at creation.
- Add tombstoned, intent-backed series deletion without weakening path
  containment or cleanup behavior.
- Expose history and current-edition navigation.

**Gate:** creating Data version 2 leaves Data version 1 semantic artifacts and
contained audit bytes unchanged after sealing; a failed or abandoned pending
edition cannot switch the current pointer early, and partial deletion resumes
only its enumerated targets.

### Phase 5 - Rebind the composite recipe

- Implement exact dataset/column compatibility and deterministic issue
  fingerprints.
- Persist operator binding overrides and recovery state in
  `RecipeApplicationDraft`.
- Rebuild supported source preparation, derived entity rules, reference bundles,
  quality rules, approved coverage, and governance against new local IDs.
- Rebuild mapping datasets, scalar fields, relationships, identities, scope,
  dispositions, write fields, and reusable control definitions; require fresh
  edition control expectations.
- Validate only required current Odoo dependency semantics and capture current
  target-reference evidence where categorical policy needs it.
- Persist a fresh mapping working draft only after complete structural binding.
- Add `RECIPE_APPLIED` audit with recipe, application-draft, and new mapping
  draft hashes.

**Gate:** compatible reordered columns rebind only when every formula uses
logical columns; confirmed renamed bindings survive refresh, duplicate headers
are rejected by source confirmation, and incompatible changes fail closed with
a direct recovery action.

### Phase 6 - Complete exception recovery and regenerate evidence

- Integrate structural and categorical compatibility issues into the existing
  mapping validation and exception-focused UX.
- Reuse the Match values UI for new scalar/many2one choices.
- Link each issue to the affected mapping field and keep blockers visible
  outside filters/pagination.
- Publish Recipe v2 only after the corrected mapping is submitted.
- Run transformation impact, preparation, quality, normalization, Odoo
  comparison, preflight, and execution snapshot through their existing
  boundaries.
- Prove that no prior current pointer or approval satisfies the new edition.
- Confirm new/changed/unchanged Odoo outcomes through governed business keys.
- Confirm that a key absent from the replacement file never becomes an
  automatic delete proposal.
- Retain unknown-write stopping and reconciliation behavior.

**Gate:** `German` and `LUX` block only their affected mappings, can be mapped
without editing other fields, and appear in Recipe v2; only exact new-edition
hashes reach comparison/load and the sealed previous edition remains
independently reopenable.

### Phase 7 - UX, documentation, and qualification

- Finish grouped project cards, overview recipe status, new-data confirmation,
  exception summary, and history presentation.
- Add source-to-source added/changed/unchanged/absent summaries only if they can
  be derived set-wise from governed business keys without weakening evidence
  boundaries. This summary is informational and never a deletion policy.
- Update user workflow, developer workflow, evidence lifecycle, architecture
  map, glossary, authoritative roadmap when applicable, and `docs/workflow.yml`
  coverage.
- Capture fictional-data screenshots and run visual/accessibility acceptance.
- Measure project-list and recipe-application query counts and timings.
- Run the focused and complete test suites in a writable Windows temp context.

**Gate:** a data-informed non-technical operator can complete the two-version
customer scenario with one obvious next action at each step and no manual
recreation of compatible rules.

## 13. Acceptance scenarios

### 13.1 Compatible replacement

Given an eligible submitted composite Customer Recipe v1 and a complete later
file with the same logical structure, when the operator starts Data version 2,
then all supported preparation, mapping, governance, quality, coverage,
reference, and control-definition rules rebind, no old evidence becomes current,
and the operator proceeds without manual remapping apart from edition
parameters/expectations.

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

A uniquely named reordered column rebinds only when all dependent formulas use
logical column identities. An ordinal formula is rewritten or makes the recipe
ineligible. A renamed used column blocks and requires an explicit new binding;
the choice survives reload/refresh in the application draft. Duplicate headers
fail existing source confirmation. A new unused column is informational.

### 13.8 Derived and related datasets

Derived categories and relationships are recreated from the new physical
selection. They do not reuse prior derived rows or dataset IDs. Missing or
ambiguous references block preparation.

### 13.9 Concurrency and interruption

Two stale **Use new files** submissions cannot allocate the same edition or
both become current. Process interruption at every creation/activation step
leaves the old edition current plus at most one exact pending edition, or one
recoverable, fully linked new active edition. A pending edition can be resumed
or safely discarded and never replaces the current card label prematurely.

### 13.10 Authorization and isolation

An actor cannot read or apply a recipe from an unrelated series and cannot use
series authority to bypass an edition's project authority. Series and project
UUIDs are not interchangeable. Recipe payloads, credentials, file paths, and
protected evidence do not leak through cards, errors, logs, or URLs.

### 13.11 Performance and N+1

Listing 100 series with multiple editions executes a bounded registry query
set and opens no edition databases. Applying a recipe performs no Odoo call per
source row or distinct value, scans each source dataset once for all required
categorical fields, and performs no parent scan per child relationship. Every
aggregate bound fails with a controlled issue rather than unbounded memory or
browser output.

### 13.12 Publication recovery and integrity

An interruption after mapping submission but before recipe publication leaves
the mapping submitted and **Use new files** unavailable. Intent recovery creates
or reuses exactly one revision and current pointer. Corrupted recipe bytes fail
the payload hash before parse; semantic-equivalent payloads deduplicate only
after both integrity and canonical semantic checks.

### 13.13 Controls, references, and quality

Data version 2 receives the reusable control calculation/tolerance but requires
a fresh expected total. Supported reference literals receive new edition-local
IDs while preserving semantic identity. Reusable quality rules run on the new
snapshots; prior quality outcomes and normalization corrections never become
current evidence or recipe rules automatically.

### 13.14 Credentials

The operator can explicitly copy only a persistent same-target read credential.
The copy creates a new secret generation and must pass a new probe before use.
Session-only, changed-target, failed-probe, and write credentials are not reused;
Data version 1 remains sealed and byte-unchanged by the copy audit.

### 13.15 Lifecycle and deletion recovery

After activation, every mutation path rejects Data version 1 even if invoked
outside the browser. Opening it is read-only and cannot change current. A crash
after any series deletion step leaves a tombstone and resumes only the exact
remaining enumerated resources; no unrelated project path is removed.

### 13.16 Legacy mapping and unsupported sources

A stored v8-v10 mapping still parses unchanged but cannot bootstrap a recipe
until categorical and control semantics receive focused review. An Odoo-origin
source, `ODOO_PINNED_UPDATE`, unsupported local reference, or nonportable formula
is rejected with a precise eligibility reason rather than partially extracted.

## 14. Expected code and documentation boundaries

Likely new modules:

- `src/impodo/domain/recipes.py` or a focused `domain/recipes/` package;
- `src/impodo/domain/project_series.py` for the aggregate above the existing
  `MigrationProject`;
- `src/impodo/application/recipe_service.py`;
- `src/impodo/application/project_edition_service.py`;
- a protected `RecipeStore` port and local adapter, separate from registry
  metadata;
- registry intent/outbox repositories and recovery services;
- `src/impodo/adapters/duckdb/project_series_repository.py`; and
- `src/impodo/web/routers/recipes.py` if project routes would otherwise become
  too broad.

Likely existing modules to change:

- `src/impodo/projects.py` for safe series-owned setup application or a narrow
  new port;
- `src/impodo/access.py` for explicit series and credential-copy capabilities;
- `src/impodo/adapters/duckdb/schema/registry.py` and registry queries;
- `src/impodo/adapters/duckdb/project_repository.py` for project-local seal and
  recipe-application-draft persistence;
- `src/impodo/domain/mapping/contracts.py` for categorical coverage policy and
  evidence, control definition/expectation separation, and the next strict
  contract version;
- `src/impodo/application/mapping_workspace_service.py` for recipe publication
  and value-coverage validation orchestration;
- preparation, derived-entity, quality, reference, and governance conversion
  services for logical rebinds;
- `src/impodo/web/app.py`, context, project/mapping presenters, routers, and
  templates;
- `src/impodo/web/target_credentials.py` and `src/impodo/secrets.py` for a
  narrow explicit same-target credential-copy operation; and
- navigation/project-list queries so internal editions do not become duplicate
  top-level projects.

Focused tests should include:

- a new `tests/test_recipes.py` and repository tests;
- registry migration, intent/outbox fault-injection, protected-store integrity,
  and aggregate-bound tests;
- `tests/test_projects.py`;
- `tests/test_workspace.py`;
- `tests/test_mapping_validation.py`;
- `tests/test_mapping_forms.py`;
- `tests/test_derived_entities.py`;
- `tests/test_readiness.py`;
- `tests/test_web_app.py`;
- `tests/test_project_security.py`;
- series/project ID confusion, per-edition authorization, historical-mutation,
  credential-copy generation/probe, and deletion-containment tests;
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

### Store full recipe JSON in the global registry

Rejected because reusable aliases, constants, reference literals, and quality
rules can be confidential business configuration. Keep only bounded metadata
and protected storage keys in the registry; payload access follows series
classification and authorization.

### Advance current when the new workspace is allocated

Rejected because upload, registration, credential setup, or recovery can still
fail. Allocation creates a pending edition; activation alone seals the prior
edition and advances the current registered pointer.

### Copy the prior read/write credential binding

Rejected because edition creation does not confer current write authority.
Only a separately authorized persistent read secret may be copied to a new
generation for the same exact target and must be re-probed. Write credentials
are established later through the normal load boundary.

### Promote observed corrections automatically

Rejected because normalization decisions and application rebindings are
edition evidence, not necessarily reusable business intent. Only explicit
promotion into a supported mapping, preparation, quality, or reference rule can
publish them in a later recipe.

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

- one visible project series with an ID distinct from every contained project
  can contain at least two immutable data versions;
- pending editions are resumable/discardable and cannot displace the current
  registered edition before activation;
- historical mutability is rejected by application/repository policy, not just
  hidden in the browser;
- an eligible submitted mapping publishes a protected, versioned recipe whose
  payload and semantic hashes are verified through a recoverable outbox;
- that recipe includes every supported reusable preparation, mapping,
  governance, quality, coverage, reference, and control-definition dependency;
- a later complete file set can reuse all compatible business rules without
  copying old evidence, project-local IDs, or prior control expectations;
- new Language selection and Country many2one choices produce focused,
  actionable blockers and can create Recipe v2 after confirmation;
- structural, schema, selection, relationship, and business-key drift fail
  closed;
- the browser keeps compatible rules green and offers one obvious next action;
- previous versions remain reopenable and cannot satisfy current readiness;
- only an explicitly confirmed persistent read credential can be copied into a
  new generation and re-probed; write credentials are never copied at creation;
- registry listing and relationship matching have no N+1 project/row behavior;
- no recipe operation writes Odoo or stores a numeric Odoo ID;
- permanent series deletion cleans every contained edition and secret through
  a tombstoned, recoverable intent and governed services;
- focused, full-suite, documentation, visual, accessibility, and Windows
  qualification evidence passes;
- current user/developer documentation is updated only after the behavior is
  implemented and verified; and
- the authoritative roadmap reflects the adopted delivery priority before
  implementation begins.
