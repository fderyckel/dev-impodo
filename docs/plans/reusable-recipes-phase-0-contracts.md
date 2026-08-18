# Reusable recipe Phase 0 contracts

## Status and authority

**Status:** Frozen proposed contract from 2026-08-18. It constrains later
recipe phases but does not describe implemented browser or persistence
behavior.

The implementation plan remains
[Reusable recipes and data versions](reusable-recipes-and-data-versions-implementation-plan.md).
This document makes its Phase 0 decisions precise enough for deterministic
fixtures and review. A later phase may change a frozen shape only by updating
this document, its fixture hashes, the decision record, and all affected
acceptance cases in the same change.

## 1. Scope

Recipe v1 supports a later complete replacement set of governed CSV/XLSX files
for the same business purpose and non-secret Odoo target. It reuses portable
configuration and creates fresh edition evidence. It does not support delta
files, inferred deletes, Odoo-origin sources, pinned Odoo updates, cross-series
catalogues, unattended execution, or credential/write authorization reuse.

The existing `MigrationProject` remains one contained evidence and governance
boundary. `ProjectSeries` is a separate aggregate above it, as recorded by
[ADR-012](../decisions/README.md#adr-012--project-series-group-contained-migration-projects).

## 2. Aggregate and lifecycle contract

### 2.1 Identity and ownership

`series_id` and every contained `project_id` are independently generated UUIDs.
Equality between them is invalid. Series routes and ports accept `series_id`;
project authorization, credentials, storage, and contained deletion accept
`project_id`. No service infers one from the other.

The series owns:

- display name and business purpose;
- source mode and source system;
- non-secret Odoo connection mode, endpoint, and database;
- intended applications and models;
- classification and retention policy;
- optimistic revision;
- current registered project, optional pending project, and edition lineage;
- current recipe pointer and protected recipe revision lineage; and
- setup-hydration state/hash for legacy projects.

The edition owns:

- its distinct `project_id`, edition number, parent project, and label;
- export/as-of date and intake status;
- files and all exact source, target, mapping, preparation, quality,
  normalization, comparison, approval, execution, and reconciliation evidence;
- project-scoped credential generations and probe evidence; and
- recipe/application provenance and bounded registry history projections.

### 2.2 States and transitions

Series states are `ACTIVE` and `DELETING`. A completed deletion removes the
series and leaves only governed non-secret receipts required by current policy.

Edition states and legal transitions are:

| From | Command | To | Required result |
| --- | --- | --- | --- |
| none | Reserve edition | `PENDING` | Reserve one number/project ID and pin the current recipe without changing current registered |
| `PENDING` | Resume | `PENDING` | Open that exact contained draft |
| `PENDING` | Discard | `ABANDONED` | Only before frozen source, active jobs, or downstream immutable evidence |
| `PENDING` | Activate registered project | `ACTIVE` | Atomically change registry edition states/current pointer; recover the project-local seal marker separately |
| `ACTIVE` | Activate successor | `SEALED` | Reject all later workflow mutations through application policy and the local seal marker |
| `ACTIVE`, `SEALED`, `PENDING`, `ABANDONED` | Tombstone series | unchanged while series is `DELETING` | Reject creation/publication/mutation and run the exact deletion intent |

The series-aware application boundary permits normal workflow mutation only on
the current active edition, plus setup/intake/registration operations permitted
for the exact pending edition. A project-local seal marker is defense in depth;
the absence of that marker never makes a non-current project mutable.

Legacy setup hydration uses `PENDING`, `READY`, and `FAILED_RETRYABLE`. The
project list does not open contained databases. The first authorized series
operation opens only the current project, copies the approved series-owned
allowlist, validates and hashes it, and changes hydration to `READY`. Recipe
publication and edition creation require `READY`.

## 3. Recipe envelope and hashing

### 3.1 Contract versions

The frozen initial versions are:

| Contract | Version |
| --- | ---: |
| Recipe envelope/definition | 1 |
| Source shape recipe | 1 |
| Source preparation recipe | 1 |
| Mapping recipe | 1 |
| Target governance recipe | 1 |
| Quality recipe | 1 |
| Reference dependencies | 1 |
| Control definitions | 1 |
| Recipe application draft | 1 |
| Categorical coverage evidence | 1 |
| Edition control expectations | 1 |
| Series/edition lifecycle | 1 |
| Cross-store intents | 1 |
| Required authoring mapping contract | 11 |

Versions 8-10 remain readable by their current mapping parser. They are not
recipe-eligible until focused review creates and submits a version 11 mapping.

### 3.2 Envelope shape

An immutable stored recipe envelope has exactly these top-level fields:

```text
recipe_contract_version
semantic_hash
payload_hash
recipe
compatibility_hints
provenance
```

`semantic_hash` is the existing Impodo `content_hash(recipe)` over canonical
JSON. `payload_hash` is `content_hash(envelope_without_payload_hash)`. Reads
verify the payload hash before parsing nested values and then recompute the
semantic hash. Hashes use lowercase `sha256:` plus 64 hexadecimal characters.

The semantic `recipe` object has exactly:

```text
contract_versions
source_shape
source_preparation
mapping
target_governance
quality
reference_dependencies
control_definitions
```

It contains no recipe revision ID, series/project ID, actor, timestamp, source
selection/snapshot hash, schema snapshot hash, random rule/reference/dataset
ID, physical stable key, ordinal, local path, storage engine, credential,
numeric Odoo ID, or evidence result.

Every semantic node has a deterministic logical ID. Logical IDs use a declared
namespace and semantic path, such as `dataset:customers`,
`column:customers.customer_code`, or `quality:customers.open_balance_nonnegative`.
Collections sort by logical ID unless a field explicitly declares semantic
order. Value matches sort by source value. Business-key components preserve
declared order.

`compatibility_hints` contains non-authoritative prior dataset labels, source
names, candidate type hints, ordered header signatures, and ordinals. Hints are
inside payload integrity but outside semantic identity. They may rank an exact
candidate; they cannot authorize duplicate-header or fuzzy rebinding.

`provenance` contains series, origin project/edition, mapping version/hash,
effective source and source snapshot hashes, target schema/governance hashes,
preparation/quality/coverage/reference hashes, original submitter identity,
publisher identity, timestamp, and publication reason. It is inside payload
integrity but outside semantic identity. Stable actor identity uses issuer and
subject, not display name alone.

### 3.3 Composite recipe shape

`source_shape` contains logical datasets and logical columns. Each dataset has
`logical_dataset_id`, operator-facing `logical_name`, `required`, and ordered
`columns`. Each column has `logical_column_id`, exact prior `source_name`,
`candidate_type_hint`, and sorted `required_by` roles. Physical IDs and ordinals
never appear here.

`source_preparation` contains only reviewed execution-neutral operations with a
round-trip fixture. Recipe v1 operation kinds are `DERIVED_ENTITY`,
`PARENT_CHILD_SPLIT`, `EXACT_JOIN`, `UNION`, `GROUP`, and `AGGREGATE`. Inputs
refer to logical dataset/column IDs. Formula conversion uses a strict logical
AST with `COLUMN`, typed `LITERAL`, and allowlisted `OPERATOR` nodes; a raw
`column_<ordinal>` expression is not recipe content. DuckDB, Polars, Parquet,
PostgreSQL, Python, SQL, and filesystem details are forbidden.

`mapping` contains dataset-to-model mappings, modes, ordered identities and
scope, scalar providers, logical transforms/formula ASTs, validations,
relationships, field dispositions, approved write fields, comparison policy,
portable value matches, and categorical policies. Recipe v1 modes are `UPSERT`,
`CREATE`, and `REFERENCE` only. Source providers use logical column IDs.
Relationships use target technical model/field names and ordered portable
business keys/scope, never numeric IDs or display names alone.

`target_governance` contains only required model, field, selection, relation,
business-key, scope, readonly-for-write, and required-field dependencies. A
canonical dependency fingerprint is calculated over that subset. An unrelated
schema change is compatible; the new operational mapping still binds the full
current schema/governance hash.

`quality` contains supported reusable manager-authored rules and approved
applicability configuration. Mapping-derived and schema-derived rules are marked
for regeneration rather than copied. Quality results, quarantine decisions,
approved exceptions, and normalization corrections are forbidden.

`reference_dependencies` contains logical reference IDs, semantic names,
contract versions, exact protected package content hashes, key/value shapes,
value kinds, classification, and effective labels. The main recipe never holds
project-local `reference_id` values. Application materializes a new
edition-local bundle from the protected content-addressed package.

`control_definitions` contains logical control ID, name, logical dataset, target
field, unit, tolerance, calculation, and whether an invariant expectation is
explicitly allowed. It excludes the origin edition's expected value by default.

## 4. Edition-only application contracts

### 4.1 Recipe application draft

`RecipeApplicationDraft` is project-local and has:

```text
contract_version
application_id
project_id
recipe_id
recipe_version
recipe_semantic_hash
recipe_payload_hash
source_selection_hash
schema_dependency_hash
target_reference_dependency_hashes
revision
state
dataset_overrides
column_overrides
issue_fingerprints
updated_by
updated_at
```

States are `BINDING`, `STRUCTURALLY_COMPLETE`, `CONSUMED`, and
`STALE_RESTART_REQUIRED`. Overrides map recipe logical IDs to current physical
dataset/column stable keys. They are revision checked and survive reload/schema
refresh when their dependency hashes still match. The draft contains no scalar
mapping semantics, target write authorization, approvals, or evidence result
and can never authorize preparation. Consuming it creates a normal fresh
`MappingWorkingDraft`.

### 4.2 Categorical policy and evidence

Mapping contract v11 adds exactly four nonblank categorical policies:

- `EXACT_TARGET_VALUE`;
- `EXPLICIT_VALUE_MATCH`;
- `EXACT_BUSINESS_KEY`; and
- `EXPLICIT_KEY_MATCH`.

Blank handling stays in the existing required/null/missing policy. New scalar
selections and choice-like relationships using **Match values** default to the
applicable explicit policy.

`CategoricalCoverageEvidence` is immutable and project-local. It binds:

```text
contract_version
mapping_content_hash
effective_source_selection_hash
source_snapshot_hashes
scan_contract_hash
provider_and_normalization_semantics_hash
target_schema_dependency_hash
target_reference_evidence
field_results
content_hash
```

`target_reference_evidence` is required when target existence or uniqueness is
a mapping-submission blocker. It binds snapshot hashes, connection-target hash,
read credential generation, principal hash, permission hash, context hash, and
required dependency IDs. Each dataset is scanned once for all covered fields.
Field results contain sorted bounded distinct values/counts and their covered or
issue outcome. Evidence uses the exact provider/transformation semantics that
runtime evaluation uses.

### 4.3 Edition control expectations

`EditionControlExpectation` contains contract version, project ID, logical
control ID, expected decimal value, source/reason, actor identity, timestamp,
and content hash. Every non-invariant required control needs a new expectation
before mapping submission. Recipe application never defaults it from origin
provenance.

The export/as-of date and any other declared edition parameter follow the same
rule: new value, current actor evidence, no silent copy from the prior edition.

## 5. Cross-store recovery contracts

Every intent has `contract_version`, operation ID, series ID, expected series
revision, actor identity, correlation ID, state, retry count, timestamps, and a
bounded error category. Payloads contain exact IDs/hashes, never secrets or raw
exception text.

| Intent | Ordered states | Idempotency key/result |
| --- | --- | --- |
| Edition creation | `RESERVED`, `WORKSPACE_CREATED`, `LINKED_PENDING`, `READY_TO_ACTIVATE`, `REGISTRY_ACTIVATED`, `PRIOR_SEAL_RECORDED`, `COMPLETED` | Series ID + reserved project ID/edition; at most one pending/current result |
| Recipe publication | `PENDING`, `PAYLOAD_STORED`, `REVISION_RECORDED`, `CURRENT_ADVANCED`, `COMPLETED` | Series ID + submitted mapping content hash; one semantic revision/current pointer |
| Credential copy | `PENDING`, `READ_SECRET_COPIED`, `REPROBED`, `COMPLETED` | Source/destination project + exact target hash + source generation; one new destination `READ` generation |
| Series deletion | `TOMBSTONED`, `TARGETS_ENUMERATED`, `DELETING`, `COMPLETED` | Series ID + tombstone revision; delete only persisted enumerated targets |

Any nonterminal state may also be `FAILED_RETRYABLE`; recovery resumes from the
last verified state. Contract/integrity/authorization violations are
`FAILED_TERMINAL` and require an explicit operator recovery action. Mapping
submission remains submitted during publication recovery. `WRITE` credentials
never enter the credential-copy intent.

Activation registers the new contained project first. One registry transaction
then marks the prior edition sealed, the new edition active, and advances the
current pointer. Series-aware policy immediately rejects old-edition mutation.
Recording the prior project-local seal marker is an idempotent recovery step;
its temporary absence does not restore authority.

Deletion persists the complete exact target list before removing anything and
tombstones the series first. Recovery never discovers targets by scanning other
project directories.

## 6. Bounds

All limits are aggregate input limits checked before persistence or rendering.
Exceeding one creates one bounded eligibility/compatibility issue; it does not
truncate semantic content silently.

| Item | Recipe v1 limit |
| --- | ---: |
| Canonical main recipe envelope | 4 MiB |
| Protected reference packages, aggregate | 64 MiB |
| Logical datasets | 100 |
| Logical columns across datasets | 10,000 |
| Preparation, mapping, quality, and control nodes combined | 10,000 |
| Reference datasets | Existing limit: 50 |
| Rows per reference dataset | Existing limit: 10,000 |
| Value matches per field/relationship | Existing limit: 1,000 |
| Distinct categorical values per field | 1,000 |
| Application dataset/column overrides combined | 10,000 |
| Compatibility/validation issues retained per application | 2,000 |
| Bounded examples per issue | 20 |
| Target candidate keys per dependency batch | 10,000 |
| Intent retry count before operator-visible escalation | 20 |

Increasing a bound requires measured memory/time evidence and a contract
revision when the accepted payload space changes.

## 7. Recipe v1 eligibility matrix

The service returns every deterministic rejection up to the issue bound rather
than hiding the action or stopping at the first unsupported construct.

| Input or construct | Recipe v1 | Required handling |
| --- | --- | --- |
| Registered current `FILE` edition | Eligible | Exact current submitted mapping and current dependencies required |
| `ODOO` source binding | Rejected | `RECIPE_SOURCE_MODE_UNSUPPORTED` |
| Mapping contract v11 | Eligible | Categorical and control semantics must be complete |
| Mapping contracts v8-v10 | Review required | Focused upgrade and new checked/submitted v11 mapping |
| `UPSERT`, `CREATE`, `REFERENCE` | Eligible | Retain exact target governance dependencies |
| `ODOO_PINNED_UPDATE` | Rejected | `RECIPE_MAPPING_MODE_UNSUPPORTED` |
| Current submitted mapping | Eligible | Submission hash must match current revision/source/schema |
| Working, stale, or incomplete mapping | Rejected | `RECIPE_MAPPING_NOT_CURRENT` |
| Unique exact column name | Eligible | Candidate type remains a hint |
| Renamed column | Application review | Persist explicit application override |
| Duplicate source headers | Rejected before recipe application | Existing source confirmation remains authoritative |
| Candidate type inference drift | Review by default | Block only demonstrated provider/transform incompatibility |
| Logical formula AST | Eligible | Materialize through reviewed converter |
| Unconverted `column_<ordinal>` formula | Rejected | `RECIPE_FORMULA_NOT_PORTABLE` |
| Supported preparation operation with round-trip fixture | Eligible | Logical inputs/outputs only |
| Unsupported preparation/derived operation | Rejected | `RECIPE_PREPARATION_UNSUPPORTED` |
| Mapping/schema-derived quality rule | Eligible by regeneration | Do not copy its result or generated physical IDs |
| Supported manager-authored quality rule | Eligible | Convert logical dependencies exactly |
| Unsupported manager-authored/advanced quality rule | Rejected | `RECIPE_QUALITY_RULE_UNSUPPORTED` |
| No approved coverage and no coverage-dependent rule | Eligible | Store explicit `NOT_CONFIGURED` applicability |
| Current approved coverage | Eligible | Rebind configuration; regenerate evidence |
| Required/stale/incomplete coverage | Rejected | `RECIPE_COVERAGE_INCOMPLETE` |
| Supported protected reference package | Eligible | Republish by logical ID/content hash; materialize fresh local ID |
| Project-local or unsupported reference dependency | Rejected | `RECIPE_REFERENCE_NOT_PORTABLE` |
| Reusable control definition | Eligible | Exclude prior expected value |
| Missing new edition expectation | Application blocker | `RECIPE_CONTROL_EXPECTATION_REQUIRED` |
| Required target dependency unchanged | Eligible | Bind fresh full schema/governance hash |
| Unrelated target schema change | Eligible | Do not compare only whole-schema hash |
| Missing/incompatible required target dependency | Blocked | Exact target field/governance compatibility issue |
| Persistent same-target `READ` credential | Separately optional | Explicit copy capability, new generation, mandatory re-probe |
| Session-only, changed-target, or `WRITE` credential | Never copied | Normal later credential workflow |

## 8. Frozen acceptance fixture

The machine-readable files under `fixtures/recipes/phase-0/` define one
fictional customer migration:

- Data version 1 totals 4,700,000.00 and contains customers CUST-001,
  CUST-002, and CUST-003;
- Data version 2 totals 5,100,000.00, changes CUST-001, leaves CUST-002
  unchanged, adds CUST-004, and omits CUST-003 without implying deletion;
- `German` is a new explicit language choice and `LUX` is a new explicit
  country alias/business-key choice;
- variants cover renamed/reordered columns, an ordinal formula portability
  failure, duplicate headers, candidate type drift, stale target selection,
  missing/ambiguous relationship targets, reference-package drift, a quality
  rule, and a new unused column;
- recipe control configuration has no 4,700,000.00 expectation; each edition
  has its own expectation; and
- series, recipe, application, coverage, and intent examples use fictional
  identifiers and contain no credentials, paths, numeric Odoo IDs, or source
  rows inside the recipe.

The focused contract test recomputes canonical hashes, checks stable ordering,
proves provenance changes do not change semantic identity, rejects incidental
UUIDs in semantic content, verifies CSV row deltas/control totals, and asserts
that every required Phase 0 drift scenario has an expected outcome.

## 9. Phase boundary

Phase 0 freezes design and examples only. It does not add registry tables,
browser actions, mapping contract v11 parsing, recipe application, credential
copy, or deletion behavior. Those remain unavailable until their implementation
phase and focused tests land.

The current authoritative roadmap keeps related/mixed 100,000-row preparation
as the unconditional priority. Phase 1 must not begin until product ownership
explicitly adopts a different priority in
[Impodo remaining work](remaining-work.md), or the existing unconditional gate
is completed.

## Related documentation

- [Reusable recipes and data versions implementation plan](reusable-recipes-and-data-versions-implementation-plan.md)
- [Impodo remaining work](remaining-work.md)
- [Architecture decisions](../decisions/README.md)
- [Project lifecycle contract](../developer/contracts/project-lifecycle.md)
- [Workflow evidence lifecycle](../developer/contracts/evidence-lifecycle.md)
- [Quality and quarantine contract](../developer/contracts/quality-and-quarantine.md)
- [Architecture overview](../architecture/overview.md)
