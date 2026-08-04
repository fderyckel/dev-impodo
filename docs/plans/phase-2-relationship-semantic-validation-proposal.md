# Historical delivery Phase 2B proposal: relationship mapping and semantic validation

**Status:** Historical delivery proposal; accepted and implemented on 29 July 2026

**Prepared:** 29 July 2026

**Scope:** Delivery Phase 2B local-browser mapping workspace, relationship
authoring, and mapping-level semantic validation

Implementation note: the delivered Phase 2B uses the contracts, validation
rules, persistence model, submission gate, and UI sequence in this proposal.
Delivery Phase 2C.1 subsequently added scalar providers and transformations.
Governed lookup translations, mapping import/export, functional review, and
approval remain in the later delivery Phase 2C scope.

## 1. Recommendation

Implement the requested remainder as a **delivery Phase 2B** slice built around a new,
dataset-centric mapping contract:

```text
frozen source selection + captured Odoo schema
                         ↓
              visual mapping editor
                         ↓
              immutable mapping revision
                         ↓
             pure mapping compiler/validator
                         ↓
       hash-bound semantic-validation result
                         ↓
                 submitted revision
```

The browser should no longer treat a mapping as a list of independent
source-column/target-field pairs. A mapping revision should describe each
dataset's target model, mode, identities, scope, scalar fields, and logical
relationships. A pure validator should then prove that the complete mapping is
internally coherent and compatible with the exact frozen source and captured
schema before submission.

This approach preserves the product's established rules:

- non-technical users do not edit YAML;
- relationships remain business-key references rather than Odoo IDs;
- one2many input is represented by a child dataset's many2one;
- mapping and validation artifacts are deterministic and hash-bound;
- stale or ambiguous configuration fails closed;
- schema access remains one read-only `fields_get` request per explicitly
  permitted model;
- submission is not approval and grants no Odoo write capability.

## 2. Current-state findings

At the time of this proposal, the delivery Phase 2 slice provided a sound source/schema
binding, but its mapping contract was intentionally minimal.

| Area | Current implementation | Gap to close |
| --- | --- | --- |
| Mapping shape | `FieldMapping(dataset_name, source_column, target_model, target_field)` | No dataset target policy, identity, scope, relation, type, null, comparison, or resolver semantics |
| Source binding | Draft binds the frozen-selection hash | Entries use source names instead of the stable dataset and column keys already available in `SourceSelection` |
| Schema binding | Draft binds the schema hash | Workspace schema omits `relation_field`, although the connector already captures it |
| Validation | Source/target existence, readonly fields, repeated mappings | No complete mapping compiler, type compatibility, identity/scope validation, relation validation, required-field coverage, dependency graph, or structured validation evidence |
| Submission | Non-empty mappings may become `SUBMITTED` | No fresh exact-hash validation is required; `SUBMITTED` correctly is not approval |
| Versioning | A version number is incremented | The DuckDB `mapping_draft` singleton replaces the previous JSON rather than retaining immutable mapping revisions |
| Invalidation | Source/schema changes delete the current draft | Stale work is removed from the active workspace but not retained as an immutable historical artifact |
| Field cardinality | A source and target pair may appear only once globally | Target uniqueness should be per dataset; the [product vision](../product-vision.md) permits one source column to feed multiple target fields when each mapping is explicit |

The existing preflight profile and engine already implement most of the
required relationship semantics: composite and scoped identities, incoming
dataset and target-catalog resolution, many2one, many2many
`replace`/`add`/`remove`, dependency-cycle rejection, metadata compatibility,
and fail-closed comparison. Delivery Phase 2B reuses those rules through a shared
semantic layer rather than reimplementing them in web routes.

## 3. Scope boundary

### Included

- target model and `upsert`, `create`, or `reference` mode per dataset;
- source identity, target identity, and target scope authoring;
- governed target business-key and scope definitions;
- scalar mapping semantics needed to validate the whole mapping;
- incoming-dataset and existing-target relationship mapping;
- many2one and many2many relationship policy;
- one2many ownership guidance and fail-closed validation;
- composite and scoped business keys;
- deterministic compilation and semantic validation;
- structured errors, warnings, coverage, and validation hashes;
- immutable mapping revisions and exact-hash submission;
- browser review and warning acknowledgement;
- contract, repository, service, web, and acceptance tests.

Identity and scope are included even though the requested emphasis is
relationships. They are prerequisites: an incoming relationship cannot carry
a referenced record's portable target identity and scope until those have
been defined.

### Excluded

- row-level normalization or data-quality execution;
- constants, defaults, transformation rules, joins, splits, aggregation, and
  expansion;
- target-record reads and relationship-resolution execution;
- durable canonical staging;
- mapping import/export;
- mapping approval signatures;
- frozen import plans;
- Odoo create, update, unlink, import, arbitrary RPC, or SQL.

At the time of this proposal, the product roadmap also listed
constants/transformations, mapping import/export, and mapping approval as
unfinished delivery Phase 2 work. This proposal therefore used the name **delivery
Phase 2B** and completed the two requested capabilities without claiming that
every item in the existing roadmap was finished. The current roadmap records
the delivered Phase 2C.1 scope separately.

## 4. Proposed contracts

### 4.1 Authoring contract

Introduce a new `MappingDefinition` rather than stretching the current
`FieldMapping` tuple or treating the existing YAML profile as a browser data
model.

Conceptual shape:

```yaml
mapping_id: 72c...
contract_version: 2
source_selection_hash: sha256:...
schema_hash: sha256:...

datasets:
  - dataset_id: dataset:4f...
    name: products
    target:
      model: product.template
      mode: upsert

    source_identity:
      column_keys:
        - column:1:...
        - column:4:...

    target_identity:
      components:
        - source_columns: [column:1:...]
          target_fields: [default_code]
          value_type: string

    scope:
      - source_columns: [column:4:...]
        target_fields: [company_id]
        resolver:
          origin: target_catalog
          model: res.company
          keys:
            - source_column: column:4:...
              target_field: x_legacy_company_code

    fields:
      - target_field: name
        source_column: column:2:...
        value_type: string
        compare: true
        required_on_create: true

    relationships:
      - target_field: categ_id
        kind: many2one
        source_columns: [column:3:...]
        resolver:
          origin: target_catalog
          model: product.category
          keys:
            - source_column: column:3:...
              target_field: complete_name
        operation: replace
        compare: true
        required: true
        on_missing: error
        on_ambiguous: error

      - target_field: tag_ids
        kind: many2many
        source_columns: [column:5:...]
        separator: ";"
        resolver:
          origin: target_catalog
          model: product.tag
          keys:
            - source_column: column:5:...
              target_field: x_legacy_tag_code
        operation: replace
        compare: true
```

Normative choices:

- source references use stable `dataset_id` and `SourceDatasetColumn.stable_key`;
- display names are retained for users but are not referential identifiers;
- one dataset declares one target model and mode;
- target-field uniqueness is enforced within a dataset, not globally across
  the project;
- one source column may feed several explicitly declared mappings;
- a target field cannot simultaneously have scalar and relation semantics;
- target model and related model are schema-derived, never accepted as
  arbitrary free text;
- target identity and target-catalog resolver fields must match an explicitly
  governed business-key definition for that model;
- no contract value contains a URL, credential, database ID, or numeric Odoo
  record ID.

### 4.2 Resolver contract

Two resolver origins are supported.

**Incoming dataset**

```yaml
resolver:
  origin: dataset
  dataset_id: dataset:parent...
```

The referenced source identity is derived from the selected dataset rather
than being retyped by the user. The compiler carries that dataset's target
model, target identity, and target scope. It adds one dependency edge.

**Existing target catalog**

```yaml
resolver:
  origin: target_catalog
  model: res.company
  keys:
    - source_column: column:4:...
      target_field: x_legacy_company_code
  scope:
    - source_column: column:6:...
      target_field: country_id
```

Keys and scope are explicit ordered mappings. This closes the documented
forward-resolution gap where `target_scope_fields` can currently render an
existing relation but cannot consume a source-side scope.

The resolver model must equal the relation model captured from Odoo metadata.
Its key and scope fields must exist in the same captured schema.

### 4.3 Relationship policies

The browser exposes only valid choices for the captured Odoo field:

| Odoo field | Authoring behavior |
| --- | --- |
| `many2one` | One logical reference or null; `replace` only |
| `many2many` | One list-valued source column; explicit separator; `replace`, `add`, or `remove` |
| `one2many` | Not directly mapped; guide the user to map a child dataset's inverse many2one |
| non-relational | Cannot be added through the relationship builder |

Common settings are `compare`, `validate_only`, `required`,
`required_on_create`, `on_missing`, `on_ambiguous`, and `null_policy`.

The compiler enforces the existing fail-closed rules:

- a compared or required relation uses `on_missing: error`;
- a compared relation uses `on_ambiguous: error`;
- `validate_only` cannot also compare;
- many2one supports only `replace`;
- many2many uses exactly one list-valued source column;
- duplicate or empty list items remain row-level validation concerns for
  delivery Phase 3;
- dependency cycles block mapping submission.

### 4.4 Shared compiled semantics

Add a pure `mapping_semantics.py` layer with target-independent values:

```text
MappingDefinition
  -> MappingCompiler
  -> CompiledMapping
  -> MappingSemanticValidator
  -> MappingValidationResult
```

Both browser-authored mappings and the current YAML profile should lower to
the same semantic rules where their feature sets overlap. The initial change
can extract reusable type, identity, relation, and metadata checks from
`profile.py` and `metadata.py`; it does not need to make the browser contract
depend on source file paths or named-table limitations in `SourceSpec`.

This is preferable to generating YAML inside a web route:

- browser mappings can refer to frozen named tables through stable artifact
  keys;
- current profiles remain a supported expert/preflight input;
- semantic parity can be contract-tested;
- Delivery Phase 4 can add a deliberate adapter from durable staged datasets to the
  preflight engine without rewriting the mapping UI.

## 5. Schema discovery changes

Relationship authoring needs slightly richer schema evidence.

Extend `SchemaField` with:

- `relation_field` for one2many inverse ownership;
- the existing `selection` values;
- a stable field key derived from model and technical field name;
- optionally, `company_dependent` when Odoo exposes it and the connector
  contract is deliberately expanded.

Do not infer business keys from field names, uniqueness guesses, or an `x_`
prefix. Add versioned `BusinessKeyDefinition` evidence beside the schema:

```text
BusinessKeyDefinition
├── key_id
├── model
├── ordered_key_fields
├── ordered_scope_fields
├── description
├── status: CANDIDATE | CONFIRMED
├── recorded_at
└── recorded_by
```

Only `CONFIRMED` definitions may be used for a dataset target identity or an
existing-target resolver. Confirmation is a governed data-manager action and
records the functional input/reference where applicable. It is not a mapping
approval signature. Changing a definition changes the schema-governance hash
and makes affected validation stale.

Use a separate `schema.govern` capability for schema scope and business-key
confirmation. `schema.discover` continues to authorize only the read-only
capture itself.

For this contract, `schema_hash` means the governed schema-bundle hash covering
the field catalog, explicit model scope, and business-key definitions—not
only the raw `fields_get` projection.

Keep model access explicit. A relationship to a model not in the current
schema must not trigger an automatic read. Add a versioned **schema scope**
step where the data manager explicitly permits related/reference models and
then recaptures the catalog. Recapture produces a new schema hash and makes
the current mapping stale.

The schema scope is a Stage C artifact. It should not silently mutate the
registered Odoo target or broaden the connector's public capability.

## 6. Semantic validation

### 6.1 Validation result

Persist a deterministic result for one exact mapping revision:

```text
MappingValidationResult
├── contract_version
├── validator_version
├── mapping_content_hash
├── source_selection_hash
├── schema_hash
├── status: VALID | VALID_WITH_WARNINGS | INVALID
├── issues[]
├── coverage[]
├── deferred_runtime_checks[]
└── validation_hash
```

Each issue contains:

- stable code and severity;
- dataset ID/name;
- source column key/name where applicable;
- target model and field where applicable;
- a JSON-pointer-like contract path;
- business-readable message and remediation;
- no raw source-row value.

Issues, coverage rows, and all nested mappings have deterministic ordering.
`validation_hash` covers the complete result except itself.

This validation proves that the **mapping meaning** is coherent. It cannot
prove facts that require reading every source or target record: actual source
key uniqueness, post-normalization collisions, non-null row values, target
key uniqueness, or successful reference resolution. Those checks are listed
explicitly in `deferred_runtime_checks` and become delivery Phase 3/4 gates. A static
mapping result must never describe them as passed.

### 6.2 Validation layers

1. **Binding**
   - exact source-selection and schema hashes still exist;
   - referenced dataset and column keys exist;
   - referenced models and fields exist in the permitted schema.

2. **Dataset**
   - every dataset has one target model and valid mode;
   - create mode has `on_existing`;
   - reference mode produces no future import action;
   - each source dataset appears at most once unless a later explicit
     expansion contract is introduced.

3. **Identity and scope**
   - source and target identities are non-empty;
   - ordered arity is valid;
   - identity/scope target fields exist and are type-compatible;
   - complete target identity and scope match a confirmed governed business
     key definition;
   - relational identity components are many2one and point to the resolver
     model;
   - target identity fields do not conflict with scalar/relation producers;
   - the same target identity field is not repeated accidentally.

4. **Scalar field**
   - source and target fields exist;
   - selected canonical type is compatible with Odoo metadata;
   - readonly fields are rejected unless explicitly validate-only and
     semantically meaningful;
   - candidate source type is advisory: disagreement creates a warning rather
     than silently changing the type;
   - repeated target fields within one dataset are rejected;
   - explicit reuse of a source field is permitted.

5. **Relationship**
   - relation kind and related model match metadata;
   - direct one2many ownership is rejected with inverse-field guidance;
   - resolver keys/scope are non-empty, ordered, and present on the captured
     related model;
   - target-catalog resolver keys/scope match a confirmed governed business
     key definition;
   - incoming references point to a configured dataset;
   - many2many and policy combinations follow the rules in section 4.3;
   - dependency cycles are rejected.

6. **Required-field coverage**
   - each writable, required target field has a scalar or relationship
     provider;
   - readonly/computed required fields do not demand a source provider;
   - because constants/defaults are outside this slice and Odoo defaults
     cannot be proved through `fields_get`, an otherwise unprovided writable
     required field blocks submission.

7. **Completeness and staleness**
   - a submitted revision has no error issue;
   - warnings are explicitly acknowledged by issue fingerprint;
   - submission repeats validation against the exact current hashes;
   - any content, source-selection, schema-scope, or schema-catalog change
     invalidates the validation and submission eligibility.

### 6.3 Initial issue codes

| Code | Severity |
| --- | --- |
| `MAPPING_SOURCE_SELECTION_STALE` | error |
| `MAPPING_SCHEMA_STALE` | error |
| `MAPPING_DATASET_UNKNOWN` | error |
| `MAPPING_SOURCE_COLUMN_UNKNOWN` | error |
| `MAPPING_TARGET_MODEL_UNKNOWN` | error |
| `MAPPING_TARGET_FIELD_UNKNOWN` | error |
| `MAPPING_TARGET_FIELD_DUPLICATE` | error |
| `MAPPING_TARGET_FIELD_READONLY` | error |
| `MAPPING_TYPE_INCOMPATIBLE` | error |
| `MAPPING_SOURCE_TYPE_ADVISORY_MISMATCH` | warning |
| `MAPPING_SOURCE_IDENTITY_MISSING` | error |
| `MAPPING_TARGET_IDENTITY_MISSING` | error |
| `MAPPING_IDENTITY_ARITY_INVALID` | error |
| `MAPPING_SCOPE_INVALID` | error |
| `MAPPING_BUSINESS_KEY_NOT_GOVERNED` | error |
| `MAPPING_BUSINESS_SCOPE_NOT_GOVERNED` | error |
| `MAPPING_REQUIRED_FIELD_UNMAPPED` | error |
| `MAPPING_RELATION_KIND_INCORRECT` | error |
| `MAPPING_RELATED_MODEL_INCORRECT` | error |
| `MAPPING_ONE2MANY_OWNER_INVALID` | error |
| `MAPPING_REFERENCE_KEY_INVALID` | error |
| `MAPPING_REFERENCE_SCOPE_INVALID` | error |
| `MAPPING_RELATION_POLICY_UNSAFE` | error |
| `MAPPING_DEPENDENCY_CYCLE` | error |

Messages are presentation text. Submission logic depends only on stable code
and severity.

## 7. Lifecycle and persistence

### 7.1 Mapping lifecycle

```text
DRAFT
  ├── validate -> INVALID
  ├── validate -> VALID_WITH_WARNINGS
  └── validate -> VALID

VALID or acknowledged VALID_WITH_WARNINGS
  └── submit exact revision -> SUBMITTED

source/schema/content change
  └── new DRAFT revision; prior submission remains historical but is STALE
```

`SUBMITTED` means ready for a later mapping-review/approval slice. It does not
mean approved and does not grant an Odoo capability.

### 7.2 Append-only storage

Replace the singleton `mapping_draft` as the authoritative store with:

```text
mapping_revision
  (mapping_id, version, parent_version, content_hash,
   source_selection_hash, schema_hash, payload_json,
   created_at, actor_issuer, actor_subject, actor_display_name)

mapping_validation
  (mapping_id, version, validator_version, validation_hash,
   result_json, created_at)

mapping_submission
  (submission_id, mapping_id, version, content_hash, validation_hash,
   warning_acknowledgements_json, submitted_at,
   actor_issuer, actor_subject, actor_display_name)

mapping_current
  (singleton_id, mapping_id, version)
```

Saving uses an expected parent version/content hash to prevent lost updates.
Submitting appends evidence for one immutable revision. Source or schema
changes move the current pointer to a new draft context; they do not erase
historical revisions or submissions.

The current proof of concept promises no released mapping compatibility.
Preserve existing version-1 draft JSON as legacy audit evidence or mark it
`LEGACY_UNVALIDATED`; do not invent relationship meaning during migration.

The project summary's `mapping_version` should be derived from the current
submitted revision rather than updated independently.

## 8. Browser experience

Use a guided, dataset-first workspace:

1. **Dataset setup**
   - choose one target model and mode;
   - show source row/column summary and schema hash.

2. **Identity and scope**
   - select or confirm a governed target business-key definition;
   - choose source identity columns;
   - build ordered target identity components;
   - add company/site/parent scope where required.

3. **Fields**
   - map source columns to writable scalar target fields;
   - choose canonical type and compare/validate-only behavior;
   - show required, readonly, type, and selection metadata inline.

4. **Relationships**
   - show relational fields only;
   - lock kind and related model from schema;
   - choose incoming dataset or existing target catalog;
   - select a confirmed resolver key, configure its ordered source
     key/scope mappings, and set many2many behavior;
   - display the dependency graph and one2many ownership guidance.

5. **Validate and review**
   - show errors first, then warnings and coverage;
   - link every issue to the relevant editor section;
   - show exact source-selection, schema, mapping, and validation hashes;
   - permit submission only after a fresh valid result and required warning
     acknowledgements.

Draft saves may contain incomplete work. Validation should run after each save
and also on demand, but only submission is a gate.

## 9. Delivery plan

Keep the work in reviewable slices.

### Slice 1 — Contracts, schema evidence, and persistence

- add `MappingDefinition`, stable authoring value objects, and canonical hash;
- add `relation_field` to the workspace schema;
- add explicit versioned schema scope, governed `BusinessKeyDefinition`
  evidence, and a schema-bundle hash;
- add and audit the `schema.govern` capability;
- migrate DuckDB to append-only mapping revisions, validations, submissions,
  and current pointer;
- retain existing legacy draft evidence without semantic conversion;
- add optimistic expected-version checks.

Gate: one deterministic incomplete mapping can be saved and reloaded without
losing history.

### Slice 2 — Compiler and semantic validator

- add pure compiler and validation-result contracts;
- extract/reuse type and relation compatibility from the current profile and
  metadata validators;
- implement identity, scope, field, required coverage, relation policy,
  resolver, and dependency-graph validation;
- implement deterministic issue ordering and validation hashing;
- add parity fixtures shared with the YAML profile semantics.

Gate: malformed or stale mapping content cannot produce a valid result.

### Slice 3 — Dataset, identity, scope, and scalar UI

- replace the flat table with dataset-centric pages;
- use stable dataset/column keys in forms;
- add target mode, identity, scope, canonical type, and scalar policies;
- allow explicit source reuse and enforce per-dataset target uniqueness;
- keep all existing loopback, session, Origin, CSRF, and form-allowlist
  controls.

Gate: a complete scalar-only mapping can validate without YAML.

### Slice 4 — Relationship UI

- add metadata-driven many2one and many2many builders;
- add incoming-dataset and target-catalog resolvers;
- add composite/scoped keys and source-side target scope;
- add one2many inverse guidance and dependency-graph display;
- add schema-scope expansion flow for explicitly permitted reference models.

Gate: the documented product, company, tag, and parent/child examples can be
authored and validated through the browser.

### Slice 5 — Review, submission, and hardening

- add validation review, issue navigation, coverage, and warning
  acknowledgement;
- require fresh exact-hash validation in the submit command;
- append immutable submission and actor evidence;
- update project summary and audit views;
- complete browser, repository, security, determinism, and documentation
  tests.

Gate: a stale, invalid, or unacknowledged mapping cannot be submitted, and a
submitted revision remains review evidence only.

## 10. Test and acceptance plan

### Contract and compiler

- canonical mapping and validation hashes are deterministic;
- unknown keys and unsupported enum values fail;
- stable IDs survive display-name changes;
- source reuse is allowed only through separate explicit entries;
- duplicate target fields fail per dataset but not across datasets;
- browser and YAML fixtures with equivalent supported semantics produce the
  same compiled identity/relation projection.

### Identity and scope

- scalar and composite target identities;
- company/site/parent scope;
- relational identity component;
- missing source or target identity;
- invalid arity and duplicate identity fields;
- same business key in two scopes;
- unconfirmed target identity or resolver business key;
- confirmed composite key with ordered scope.

### Relationships

- incoming many2one parent;
- target-catalog many2one;
- composite and scoped target-catalog key;
- many2many `replace`, `add`, and `remove`;
- unsafe missing/ambiguous warning policies;
- wrong relation kind or related model;
- direct one2many with and without captured inverse;
- missing reference model/key field;
- blocked dependency and dependency cycle;
- reference-only dataset.

### Persistence and invalidation

- every save creates an immutable revision;
- simultaneous stale save is rejected;
- validation binds one exact revision;
- submission binds the mapping and validation hashes;
- source reinspection, refreeze, schema-scope change, or recapture makes the
  current mapping stale without deleting history;
- legacy version-1 draft evidence remains readable but cannot be submitted.

### Browser and security

- the complete flow works with realistic CSV and XLSX named-table selections;
- relational options are derived only from the captured permitted schema;
- no relationship action performs `search_read` or another record read;
- no route exposes create/write/unlink/import/generic Odoo calls;
- hostile labels/names remain escaped;
- state changes retain session, same-origin, CSRF, Fetch Metadata, and strict
  form-field checks;
- validation results contain schema/source names but no raw row values,
  credentials, or numeric Odoo IDs.

## 11. Definition of done

Delivery Phase 2B is complete when:

- a data manager can configure dataset targets, identities, scopes,
  many2one, parent/child, and many2many mappings without YAML;
- dataset identities and target-catalog resolvers use confirmed, versioned
  business-key definitions rather than inferred key fields;
- target-catalog relations support explicit composite and source-side scoped
  business keys;
- direct one2many writes are prevented and explained;
- one pure validator covers source bindings, schema compatibility, identities,
  fields, required coverage, relations, policies, and cycles;
- validation and submission bind exact source, schema, mapping, and validator
  hashes;
- every issue has a stable code, severity, location, and remediation;
- invalid or stale mappings cannot be submitted;
- warning acknowledgement is explicit and hash-bound;
- mapping revisions, validations, submissions, and actors are retained
  immutably;
- equivalent browser/YAML semantics have parity tests;
- all existing preflight and local-browser tests remain green;
- the connector remains read-only and no Odoo record ID enters a portable
  mapping or validation artifact;
- documentation continues to state that submission is not approval.

## 12. Decisions to confirm before implementation

Recommended defaults are shown first.

1. **Roadmap label at the time:** call this delivery Phase 2B and retain
   constants, transformations, import/export, and approval as delivery Phase 2C.
2. **Required fields:** block an unprovided writable required field; do not
   assume an unproven Odoo default.
3. **Warnings:** require acknowledgement of exact warning fingerprints before
   submission.
4. **Schema expansion:** require explicit schema-scope permission and
   recapture; never auto-read a related model.
5. **Version history:** retain stale revisions and submissions; invalidate
   eligibility rather than deleting evidence.
6. **Compatibility:** preserve existing flat drafts as legacy evidence but do
   not silently translate them into semantic mappings.
7. **Business-key governance:** require confirmed key definitions and record
   the supporting functional reference; do not infer keys from technical field
   names.
