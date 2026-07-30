# Data-quality rules implementation plan

## 1. Purpose and status

This plan introduces governed source-data rules into the read-only migration
preflight engine. The objective is not merely to reformat cells. It is to
ensure that every proposed import value satisfies an approved, auditable
data-quality policy before it can be considered releasable.

This is the normalization and validation portion of the
[end-to-end migration product](product-vision.md). Source/workbook discovery,
interactive mapping, durable staging, approval, execution, and reconciliation
are defined there and are not silently included in this rules slice.

**Status:** Proposed for implementation and architecture approval.

The normative breadth and release criteria are defined by the
[data transformation coverage contract](contracts/data-transformation-coverage.md).
The early slices in this plan deliberately start with low-risk source-field
rules. The later slices close the structural transformation, entity
resolution, domain validation, Odoo semantic, and clean-package gaps identified
by that catalogue.

The feature remains inside the current safety boundary:

- it reads source data and target evidence;
- it may prepare corrected proposed values;
- it produces decisions and review evidence;
- it does not create, update, delete, or import anything into Odoo.

The intended flow is:

```text
raw source row
→ governed source-field rules
→ typed prepared record
→ post-correction identity and duplicate checks
→ target matching and exact comparison
→ row decisions
→ package quality gate
→ manifest and business-review workbook
```

## 2. Required outcomes

The implementation MUST provide:

1. governed rules attached to source fields;
2. deterministic rule ordering and repeatable results;
3. explicit `correct`, `warn`, and `reject` actions;
4. safe whitespace and case rules;
5. a bounded structured-format rule for values such as product codes;
6. post-correction duplicate detection;
7. exact rule evidence without changing the five row classifications;
8. a package-level `PASS`, `REVIEW_REQUIRED`, or `BLOCKED` quality gate;
9. separation between source correction and target comparison;
10. manifest, workbook, CLI, documentation, and test coverage;
11. a governed path for a data manager to propose, test, approve, and publish
    rules;
12. traceable delivery against every applicable transformation case family;
13. quarantine and reprocessing without modifying frozen source evidence;
14. a clean-package gate that is distinct from mapping validity, rule-engine
    success, and Odoo execution approval.

The initial proof of concept MUST NOT provide:

- arbitrary code, formulas, callbacks, or server-side expressions;
- unrestricted regular expressions;
- silent correction of identifiers without an explicit approved rule;
- automatic correction of legal names, brands, or human names by default;
- automatic fuzzy matching, record merging, or survivor selection;
- silent row dropping, join multiplication, truncation, lookup guessing, or
  unmatched-record loss;
- an approval signature or executable Odoo import plan;
- an Odoo write capability.

### 2.1 Coverage and delivery boundaries

The coverage contract has 24 case families. A product-wide 95% claim requires
at least 23 to be verified, with every mandatory high-risk family verified.
`PARTIAL`, `DESIGNED`, and `GAP` do not count.

This plan owns the rule compiler, evaluator, evidence, quality gates, and their
integration with prepared records. Some catalogue families cross product
boundaries:

- table joins, unions, pivot/unpivot, grouping, aggregation, and expansion
  integrate with the delivery Phase 3 staging/transformation compiler;
- fuzzy entity resolution and survivorship integrate with data-manager review;
- Odoo semantic certification integrates with captured schema, read-only
  target preflight, and DEV/TEST rehearsal;
- clean-package certification integrates with staging, approval, and
  reconciliation.

Those boundaries do not remove the cases from the coverage obligation. Each
owning component must implement the shared contracts, evidence, and gates
defined here and in the coverage catalogue.

## 3. Architectural decision

### 3.1 Apply rules once per source field

Rules SHOULD be declared at dataset source-field level, not independently on
each target mapping.

One source column can currently participate in:

- source identity;
- target identity or scope;
- a scalar target field;
- a many2one or many2many reference key.

Applying separate rules at each use would allow the same raw cell to acquire
different meanings. Instead, the engine will create one internal
`GovernedSourceRow` per row. Every later mapping consumes the same governed
source value.

Proposed profile shape:

```yaml
datasets:
  - name: products
    source:
      file: products.csv

    source_rules:
      product_code:
        - id: product_code_trim
          type: surrounding_whitespace
          action: correct
          description: Remove spaces before or after the product code.

        - id: product_code_uppercase
          type: case
          style: upper
          action: correct
          description: Store product-code letters in uppercase.

        - id: product_code_structure
          type: format
          template: "PROD{letter:4}{digit:3}-{digit:4}{letter:2}"
          letter_case: upper
          character_set: ascii
          action: reject
          description: >
            Require PROD, four letters, three digits, a hyphen,
            four digits and two letters.
```

`source_rules` keys MUST name source headers. They MAY govern an otherwise
unmapped column when the field is intentionally validate-only. The profile
compiler MUST reject misspelled, duplicated, contradictory, or unsupported
rule declarations.

### 3.2 Extend the current profile shape

The proof of concept has one current profile shape and no released contract
generations. Add governed semantics directly to that shape without legacy
compatibility branches.

The distinction between source rules, technical parsing, and target comparison
must remain explicit. Do not silently reinterpret the existing `normalize`
object; introduce `source_rules`, update the committed profiles and fixtures
together, and require fresh review artifacts after the change.

Recommended contract changes:

- the profile adds dataset-level `source_rules`;
- technical parsing retains null, decimal, date, datetime, and timezone
  policies;
- source-facing whitespace and case transformations move to `source_rules`;
- comparison policy is explicit and does not automatically reuse source
  transformations;
- the prepared-record contract adds rule evidence;
- the preflight-result contract adds rule summaries and the package quality
  gate;
- snapshot and connector contracts remain unchanged.

All committed profiles MUST be updated to the current shape in the same change.
Previously generated profiles and snapshots are disposable proof-of-concept
artifacts and do not require a migration path.

## 4. Rule contract

### 4.1 Common fields

Every rule MUST contain:

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier, unique within the dataset |
| `type` | Discriminator selecting a supported built-in rule |
| `action` | `correct`, `warn`, or `reject` |
| `description` | Business-readable purpose retained in evidence |

Optional governance metadata MAY include an owner, policy reference, and
effective date, but operational identities and approval signatures belong in
a later approval contract rather than the source profile.

Rule IDs MUST match a restricted identifier pattern. Rule descriptions are
data, not executable content.

### 4.2 Action semantics

| Action | Value behavior | Evidence | Row effect |
| --- | --- | --- | --- |
| `correct` | Apply a deterministic correction | `CORRECTED` event with before/after values | Does not block |
| `warn` | Retain the value | Warning issue and `WARNED` event | Does not block; package needs review |
| `reject` | Retain the value for traceability | Error issue and `REJECTED` event | Row becomes `BLOCKED` |

`correct` MUST only be accepted for rules with a unique deterministic
correction. The profile compiler MUST reject `action: correct` for a format
rule because the intended replacement cannot be inferred safely.

Passing evaluations SHOULD be aggregated rather than stored row by row.
Non-pass events MUST retain row-level traceability.

### 4.3 Initial built-in rules

The initial implementation will support:

| Rule | Parameters | Correctable |
| --- | --- | --- |
| `surrounding_whitespace` | Unicode whitespace policy | Yes |
| `collapse_whitespace` | replacement separator, default one space | Yes |
| `case` | `lower`, `upper`, `sentence`, or `title` | Yes, with restrictions |
| `format` | bounded template, letter case, character set | No |
| `forbidden_characters` | built-in control/invisible-character classes | No |

The first evaluator slice remains intentionally small. The following are
required follow-on rule families rather than optional ideas:

- length, numeric range, allowlist, denylist, checksum, and conditional
  requiredness;
- declared number, money, percentage, unit, date, datetime, boolean, and
  sentinel parsing;
- versioned dictionaries, exact lookups, selection translations, constants,
  defaults, ordered coalesce, and bounded conditions;
- split, extract, concatenate, date arithmetic, and safe calculated values;
- email, phone, country/subdivision/postcode, address, VAT/tax, IBAN/BIC,
  GTIN/EAN, and URL validation;
- cross-field, cross-row, distribution, and reconciliation rules.

Structural transforms, entity resolution, survivorship, and Odoo semantic
certification use separate domain objects because they operate across rows,
datasets, reviewer decisions, or target context. They still feed the same
issue, evidence, quarantine, and clean-package gates.

The rule framework MUST allow new discriminated rule types without adding
dataset-specific branches to the engine.

### 4.4 Case definitions

Case behavior MUST be documented and regression-tested precisely.

```yaml
# sentence:
# Uppercase the first cased letter and lowercase subsequent letters.
# "pREMIUM BLUE widget" becomes "Premium blue widget".
# Use for descriptions, not names, brands, acronyms, or identifiers.

# title:
# Uppercase the first cased letter of each word and lowercase the rest.
# "premium BLUE widget" becomes "Premium Blue Widget".
# Use cautiously: iPhone, McDonald, ACME, apostrophes, hyphens, and
# multilingual names may need exceptions or human review.
```

The initial rule set MUST define:

- whether case handling is Unicode or ASCII;
- how leading digits and punctuation are handled;
- what constitutes a word boundary for `title`;
- how apostrophes and hyphens behave;
- whether empty and null values are skipped;
- examples for every supported behavior.

Recommended defaults:

- identifiers use explicit ASCII and normally `upper` or `lower`;
- descriptive text uses Unicode;
- `sentence` and `title` are opt-in;
- legal names, customer names, brands, and product names default to `warn` or
  `reject`, not `correct`;
- locale-sensitive behavior is never inferred from the machine locale.

### 4.5 Safe structured formats

Business users SHOULD use a bounded format template instead of raw regular
expressions.

Initial format grammar:

- literal characters match exactly;
- `{letter:n}` means exactly `n` letters;
- `{digit:n}` means exactly `n` ASCII digits from `0` through `9`;
- `letter_case` is `any`, `upper`, or `lower`;
- `character_set` is initially limited to `ascii`;
- the entire value is matched; partial matches are impossible.

Example:

```yaml
- id: product_code_structure
  type: format
  template: "PROD{letter:4}{digit:3}-{digit:4}{letter:2}"
  letter_case: upper
  character_set: ascii
  action: reject
  description: >
    Require a product code such as PRODABCD123-4567XY.
```

Equivalent implementation matcher:

```regex
^PROD[A-Z]{4}[0-9]{3}-[0-9]{4}[A-Z]{2}$
```

The implementation SHOULD use a direct bounded matcher or a safely compiled
anchored expression. It MUST enforce maximum template length, maximum token
count, maximum source-value length, and deterministic runtime. Unrestricted
regex support is deferred until timeout and catastrophic-backtracking
protections have been designed and tested.

## 5. Evaluation semantics

### 5.1 Fixed processing order

Within a source field, the declared rule order is meaningful. The compiler
MUST additionally enforce these processing steps:

1. forbidden/control/invisible character validation;
2. surrounding-whitespace handling;
3. internal-whitespace handling;
4. case handling;
5. structured-format validation;
6. technical type parsing;
7. required/null validation;
8. cross-field and reference validation when later introduced.

A profile that declares an unsafe or contradictory order MUST fail to load.
Repeated execution on already-corrected data MUST be idempotent.

### 5.2 Raw, governed, and typed values

The engine will distinguish:

- `raw_value`: exact source cell value;
- `governed_value`: value after approved corrections;
- `typed_value`: parsed value used by prepared records and comparison.

The raw value is source diagnostic evidence only. It MUST NOT become an Odoo
identifier or enter target matching except through the governed and typed
stages.

### 5.3 Duplicate and reference checks

All identity, scope, and reference-key consumers MUST use governed values.
Duplicate source identities MUST be detected after correction.

This prevents the engine from treating these as two safe records:

```text
prodabcd123-4567xy
PRODABCD123-4567XY
```

when the approved policy uppercases both values.

An incoming reference MUST resolve using the same governed representation as
the referenced dataset. Conflicting rules on both sides of a relationship
MUST be detected during profile compilation where possible.

### 5.4 Source correction versus target comparison

Source rules define the desired proposed value. They MUST NOT be
automatically applied to an existing Odoo value during comparison.

Example:

```text
governed source: "Acme"
existing target: " Acme "
```

The result SHOULD be a material difference if the desired stored value is
`"Acme"`. Shared normalization would incorrectly hide the target defect.

Target parsing may still perform technical conversions required to represent
booleans, decimals, dates, datetimes, Odoo false/null values, and explicit
comparison equivalence. Any case-insensitive or whitespace-insensitive target
comparison MUST be a separate, visible comparison policy.

## 6. Evidence model

### 6.1 Domain objects

Add environment-independent domain objects:

```text
RuleEvent
├── rule_id
├── rule_type
├── outcome: CORRECTED | WARNED | REJECTED
├── dataset
├── source_row
├── source_field
├── before
├── after
└── message

RuleSummary
├── rule_id
├── dataset
├── source_field
├── evaluated
├── passed
├── corrected
├── warned
└── rejected
```

`PreparedRecord` will carry only its non-pass rule events. `PreparedBundle`
will carry deterministic aggregated summaries. Error and warning rule events
will also produce the existing structured `Issue` objects so classification
precedence remains unchanged.

### 6.2 Sensitive data

Before/after evidence can contain personal or commercially sensitive data.
The profile contract SHOULD support an evidence policy per governed field:

```yaml
evidence: full    # full | masked | none
```

Recommended defaults:

- identifiers and non-sensitive codes: `full`;
- customer or personal fields: `masked`;
- secrets and prohibited data: `none` and reject at ingestion.

Masking MUST be deterministic enough for review but MUST NOT expose the full
value. The manifest and workbook must use the same portable evidence policy.
Logs and exceptions MUST never include unredacted values by accident.

## 7. Package quality gate

Rule outcomes do not add a sixth row classification. The existing
`CREATE`, `UPDATE`, `UNCHANGED`, `AMBIGUOUS`, and `BLOCKED` outcomes remain.

The preflight result adds:

```text
quality_gate.status:
  PASS
  REVIEW_REQUIRED
  BLOCKED
```

Initial package policy:

- one or more rejected/error rows → `BLOCKED`;
- no errors but one or more warnings → `REVIEW_REQUIRED`;
- corrections are always reported;
- correction policy may be `report` or `review_required`;
- no applicable non-pass outcome → `PASS`;
- `AMBIGUOUS` and row-level `BLOCKED` remain package-blocking regardless of
  whether they originated in data-quality rules.

The command can complete successfully and still produce a blocked package.
Existing CLI exit code `0` continues to mean that valid review artifacts were
produced. Add a separate enforcement option or command for automated release
pipelines so a blocked or review-required quality gate produces a stable
non-zero policy exit code without conflating data findings with engine
failure.

This milestone reports `REVIEW_REQUIRED`; it does not record an approval
signature. Approval belongs to the future approval-manifest milestone.

## 8. Reporting and operator experience

### 8.1 Manifest

The preflight result MUST include:

- profile ID and a canonical profile/ruleset hash;
- package quality-gate status and reasons;
- rule summaries;
- non-pass rule events subject to evidence policy;
- unchanged row decisions and differences;
- existing source and snapshot hashes;
- the engine name.

The semantic hash MUST cover the complete rule configuration and conclusions.
Relying only on the profile ID is insufficient for governed rules.

### 8.2 Workbook

Add a thirteenth sheet, `Rule Results`, containing:

- outcome;
- rule ID and description;
- dataset, source row, and source field;
- protected before/after values;
- message;
- affected-row count where grouped.

Update the Dashboard with:

- package quality-gate status;
- evaluated, corrected, warned, and rejected counts;
- correction and rejection rates by dataset;
- the highest-impact failing rules.

The existing `Source Issues` sheet remains the authoritative error/warning
projection. The `Rule Results` sheet explains rule-specific evidence.
Existing spreadsheet-formula-injection protection MUST be reused for every
new cell.

### 8.3 Manager-facing rule authoring

Core rule execution SHOULD be delivered before a non-technical editor.
The approved YAML profile remains the runtime authority.

The manager-facing workflow will be:

1. create or edit a draft rule through controlled fields;
2. validate the draft profile contract;
3. preview it against a representative source package;
4. review changed, warned, rejected, and collision counts;
5. approve the business meaning outside the engine;
6. publish an immutable profile artifact identified by its content hash;
7. retain the approved profile and ruleset hash with the review package.

The first authoring surface MAY be a controlled spreadsheet or small form, but
it MUST expose enumerated rule types and parameters rather than arbitrary
YAML, regex, or code. Publishing must generate a strict profile that is
validated by the same loader used at runtime.

During the proof of concept, a profile may be edited in place. Every rule
change requires a new preview, produces a new profile/ruleset hash, and
invalidates earlier generated review artifacts.

## 9. Delivery slices

### Slice 0 — Approve contracts and semantics

Deliver:

- this plan reviewed by engineering, data management, and security;
- exact current profile, prepared-record, and result schemas;
- case, whitespace, format, Unicode, null, and evidence definitions;
- quality-gate policy and CLI behavior;
- examples for customers and products;
- a rule-risk matrix defining where `correct` is permitted.

Gate:

- no ambiguous rule semantics remain;
- product-code casing and case sensitivity are explicitly approved;
- evidence retention and masking are approved.

### Slice 1 — Profile contract and rule compiler

Primary modules:

- `profile.py`;
- new `rules.py` or equivalent domain module;
- profile contract and authoring documentation.

Deliver:

- strict discriminated Pydantic rule models;
- dataset-level `source_rules`;
- rule ID uniqueness;
- type/action compatibility validation;
- rule ordering and contradiction validation;
- bounded format-template parser;
- canonical profile/ruleset serialization and hash.

Tests:

- valid rule of every initial type;
- unknown rule/type/parameter rejection;
- duplicate rule IDs;
- `correct` rejected for non-correctable rules;
- invalid processing-step order and contradictory case rules;
- bounded-template limits;
- profiles without source rules retain their existing conclusions;
- profile/ruleset hash determinism.

Gate:

- malformed or unsafe rule configuration cannot reach source processing.

### Slice 2 — Source-rule evaluator and evidence

Primary modules:

- `canonical.py`;
- new rule evaluator;
- `models.py`.

Deliver:

- `GovernedSourceRow`;
- deterministic rule evaluation;
- `RuleEvent` and `RuleSummary`;
- full/masked/none evidence policy;
- whitespace, case, format, and forbidden-character rules;
- explicit issue creation for warn/reject;
- idempotence checks.

Tests:

- leading/trailing ordinary and Unicode whitespace;
- collapse behavior;
- lower/upper/sentence/title examples;
- punctuation, leading digits, apostrophes, and hyphens;
- ASCII product-code examples;
- null and empty handling;
- control and invisible characters;
- evidence masking;
- no raw value in logs/errors;
- repeat evaluation yields the same governed value.

Gate:

- every non-pass rule evaluation is traceable and deterministic.

### Slice 3 — Prepared-record integration

Primary modules:

- `source.py`;
- `planner.py`;
- `engine.py`.

Deliver:

- rules applied before every consumer of a source field;
- scalar mappings, identities, scopes, and references use governed values;
- duplicate detection runs after correction;
- blocked parent/reference propagation remains fail-closed;
- source rules are not reused on target values;
- technical target parsing remains explicit;
- existing comparison behavior remains stable where source rules do not apply.

Tests:

- two raw identities collide after uppercasing;
- corrected parent and child keys resolve consistently;
- rejected identity blocks before matching;
- product-code format applies when the column is used only as identity;
- dirty target whitespace produces a material difference with source rules;
- existing normalization still produces its previous result when no source
  rule applies;
- request planning uses governed keys and remains batched.

Gate:

- no raw or inconsistently governed source value participates in matching,
  resolution, or comparison.

### Slice 4 — Package quality gate and CLI

Primary modules:

- `engine.py`;
- `models.py`;
- `cli.py`;
- result contract and operating documentation.

Deliver:

- package `PASS`, `REVIEW_REQUIRED`, or `BLOCKED`;
- deterministic reason codes;
- correction policy;
- summary reconciliation;
- source-profile preview output with rule counts;
- explicit automated quality-gate enforcement option;
- stable policy exit codes distinct from engine failure.

Tests:

- each gate status;
- warnings require review but do not block individual rows;
- rejected rules block rows and package;
- ambiguous and other blocked outcomes block the package;
- correction-policy variants;
- CLI still writes review artifacts for a blocked package;
- enforcement option exits according to policy.

Gate:

- an automated release process cannot mistake a valid report for a releasable
  import package.

### Slice 5 — Manifest and workbook evidence

Primary modules:

- `reporting.py`;
- `resources/build_review_workbook.mjs`;
- reporting tests.

Deliver:

- preflight-result serialization;
- `Rule Results` workbook sheet;
- Dashboard quality-gate and rule metrics;
- protected before/after values;
- profile/ruleset hash;
- reconciliation between manifest events, issues, decisions, and workbook;
- deterministic JSON.

Tests:

- exact sheet names and headers;
- rule summary totals reconcile with events and issues;
- all protected values are safe from formula injection;
- sensitive values obey evidence policy;
- manifest contains no numeric Odoo IDs or secrets;
- unchanged inputs produce byte-identical JSON;
- a rule change changes the ruleset and semantic hashes.

Gate:

- a reviewer can explain every correction, warning, rejection, collision, and
  package-gate conclusion using only retained artifacts.

### Slice 6 — Manager rule-authoring workflow

Deliver:

- controlled rule catalog/editor with enumerated choices;
- business-readable inline descriptions and examples;
- draft validation;
- sample-package preview;
- collision and impact analysis;
- immutable profile-artifact publication;
- export of the exact generated YAML and ruleset hash;
- access-control and change-log requirements.

Tests:

- generated profiles pass the production profile loader;
- invalid combinations cannot be published;
- published profile artifacts cannot be edited in place;
- preview and runtime use the identical generated profile;
- round-trip export does not change rule meaning.

Gate:

- the data manager can propose and test rules without writing executable
  expressions, while publication remains governed and reproducible.

### Slice 7 — Acceptance hardening and rollout

Deliver:

- expanded golden slice with rule pass/correct/warn/reject cases;
- 100–300-row sanitized deployment acceptance package;
- historical-scale performance and memory results;
- Unicode and multilingual test corpus;
- privacy review of evidence;
- operator runbook and incident procedure;
- change notes and an updated operator guide.

Gate:

- all automated and business acceptance cases pass;
- data management signs off on rule semantics and example outcomes;
- security signs off on pattern safety, evidence handling, and report output;
- profiles without source rules retain their existing conclusions;
- no write capability has been introduced.

### Slice 8 — General validation and reference-value rules

Primary modules:

- `profile.py`;
- rule compiler/evaluator;
- mapping-to-staging compiler;
- dictionary/reference-data adapter.

Deliver:

- length, range, allowlist, denylist, checksum, and conditional-required rules;
- explicit sentinel/null and boolean token policies;
- strict locale-aware number, money, percentage, unit, date, and datetime
  parsing;
- versioned exact dictionaries, lookup translations, and Odoo selection keys;
- constants, source fallbacks, leave-unset/Odoo-default intent, ordered
  coalesce, and bounded if/then/else;
- one canonical implementation of the delivery Phase 2C.1 browser-authored scalar
  providers and transformations, reused by preview and full-row execution.

Tests:

- invalid locale, grouping, sign, scientific notation, precision, and rounding;
- ambiguous dates, Excel date systems, timezone offsets, and daylight-saving
  boundaries;
- null/empty/zero/false/sentinel distinctions;
- unknown and duplicate lookup keys;
- fallback only after the declared empty-to-null policy;
- selection labels rejected unless explicitly translated to technical keys;
- preview and runtime produce the same result for the same raw value.

Gate:

- every mapped scalar value is reproducibly constructed, typed, and validated
  without guessing a locale, lookup, default, or Odoo selection value.

### Slice 9 — Structural and derived transformations

Primary modules:

- new transformation-plan domain module;
- staging compiler and store;
- row-lineage and reconciliation contracts.

Deliver:

- select, rename, copy, drop, reorder, filter, and quarantine;
- split, bounded extract, concatenate, date arithmetic, and safe calculations;
- join, append/union, pivot, unpivot, group, aggregate, row expansion, and row
  combination;
- explicit join cardinality and unmatched-row policies;
- stable trace identities for every constructed row;
- row-count equations and control-total reconciliation for each transform.

Tests:

- one-to-one, one-to-many, and many-to-many join cardinalities;
- unexpected join multiplication blocks;
- unmatched rows are retained or quarantined according to policy;
- quoting, escaping, empty tokens, ordering, and duplicates in split values;
- aggregation null, precision, rounding, and conflict policies;
- pivot/unpivot and expansion preserve lineage and reconcile counts.

Gate:

- no structural transform can silently drop, duplicate, combine, or invent a
  row without deterministic lineage and reconciliation evidence.

### Slice 10 — Domain-quality validators

Primary modules:

- new domain-validator registry;
- optional versioned reference-data provider ports;
- masking and evidence policy.

Deliver:

- email, phone, country/subdivision/postcode, VAT/tax, IBAN/BIC, GTIN/EAN, URL,
  and organization-approved validators;
- explicit region/context inputs where semantics depend on country;
- optional postal-address verification behind an approved provider interface;
- validator version and reference-data version in semantic hashes;
- clear separation between syntax, checksum, reference match, and external
  deliverability/verification.

Tests:

- valid, invalid, incomplete, and ambiguous examples from multiple countries;
- leading zeros and punctuation preserved where meaningful;
- provider unavailable, expired reference data, and unsupported country;
- sensitive values masked in evidence and absent from logs;
- no external verification result is guessed or silently downgraded to valid.

Gate:

- each enabled domain validator states exactly what it proves, what it does not
  prove, and which versioned evidence produced the result.

### Slice 11 — Entity resolution and survivorship

Primary modules:

- new entity-resolution domain module;
- candidate-index adapter;
- reviewer-decision and survivorship contracts;
- staging integration.

Deliver:

- deterministic blocking keys and bounded candidate generation;
- versioned exact, Levenshtein, Jaro-Winkler, token, or other approved match
  methods;
- per-field weights, thresholds, scores, alternatives, and language limits;
- explicit `MATCH`, `NOT_A_MATCH`, and `DEFER` reviewer decisions;
- field-level survivorship using approved source priority, recency,
  completeness, deterministic rule, or explicit reviewer choice;
- immutable cluster, decision, source-row, and golden-record lineage.

Tests:

- exact duplicates, likely duplicates, homonyms, false positives, false
  negatives, transliteration, accents, and unsupported scripts;
- threshold changes alter hashes and require new review;
- all-to-all comparison is prevented at scale by governed blocking;
- no cluster is merged and no survivor is selected without the required
  decision;
- conflicting field values remain visible.

Gate:

- zero unreviewed fuzzy candidates and zero unexplained survivor fields may
  enter an import-candidate package.

### Slice 12 — Odoo semantic and clean-package certification

Primary modules:

- Odoo semantic validator;
- staging/preflight integration;
- package certificate and quarantine workflow;
- DEV/TEST rehearsal adapter.

Deliver:

- External ID strategy, uniqueness, stability, and relationship use;
- selection technical-key validation;
- company-dependent and multi-company relationship checks;
- currency, rounding, unit-of-measure, translation, and archive-state context;
- readonly, computed, related, inverse, Odoo-default, and custom-constraint
  deferrals;
- quarantine reason, owner, expiry, correction, and rerun lifecycle;
- source/staged/candidate/quarantine counts and business control totals;
- frozen source, mapping, ruleset, schema, canonical, target, and package
  hashes;
- a clean-package certificate that remains separate from mapping submission,
  normalization approval, and execution approval;
- controlled Odoo 19 DEV/TEST rehearsal evidence.

Tests:

- External ID collisions and instability;
- selection label versus technical key;
- cross-company relationship and company-dependent field context;
- money without currency, quantity without unit, and translated value without
  language;
- Odoo-default warning remains until rehearsal;
- quarantined/excluded counts reconcile to the source;
- any changed input invalidates the certificate;
- all source rows reach exactly one terminal accounting category.

Gate:

- every applicable release gate in the
  [coverage contract](contracts/data-transformation-coverage.md#9-clean-package-release-gates)
  passes for the exact package before the data manager can label it clean for
  Odoo DEV/TEST rehearsal.

## 10. Acceptance matrix

Minimum business acceptance cases:

| Case | Expected result |
| --- | --- |
| `" PRODABCD123-4567XY "` with trim/correct | Corrected, valid, event retained |
| lowercase product code with upper/correct | Corrected before format validation |
| malformed product code | Rejected, row and package blocked |
| two codes collide after correction | Every duplicate row blocked |
| sentence case on description | Exact documented correction |
| title case on approved descriptive field | Exact documented correction |
| title case on a protected brand/name field | Profile rejected or warning-only policy |
| warning-only rule violation | Row can proceed; package review required |
| corrected source versus dirty target | Material target difference |
| invisible control character in identifier | Rejected |
| sensitive customer value violates rule | Evidence masked; issue remains actionable |
| same source/profile/rules/snapshots | Byte-identical manifest |
| one rule parameter changes | New ruleset and semantic hashes |
| profile without source rules | Current results remain unchanged |
| locale decimal without a declared locale | Rejected; locale is never guessed |
| ambiguous source date such as `01/02/2026` | Rejected until a date convention is declared |
| source label differs from Odoo selection key | Explicit lookup required; label is not imported silently |
| join expected one-to-one produces two matches | Transform and package blocked |
| filter excludes rows | Every excluded row is quarantined or explicitly reconciled |
| lookup contains two matches for one source value | Ambiguous; no value selected |
| fuzzy candidate exceeds a similarity threshold | Candidate retained for review; no automatic merge |
| two records are approved as one entity | Every survivor field retains source/rule/reviewer lineage |
| phone number lacks required country context | Warning or rejection according to the governed rule |
| valid-looking IBAN fails checksum | Rejected |
| monetary value has no currency context | Blocked before Odoo rehearsal |
| relationship crosses an invalid company boundary | Blocked |
| Odoo selection label supplied instead of technical key | Blocked unless an approved translation exists |
| quarantined rows plus candidates do not equal source rows | Clean-package certification blocked |
| exact package passes rules but not Odoo DEV/TEST constraints | Not clean for execution; package remains blocked |

Minimum product-code corpus:

```text
PRODABCD123-4567XY  valid
PRODABC123-4567XY   invalid: three letters after PROD
PRODABCD12-4567XY   invalid: two digits before hyphen
PRODABCD1234567XY   invalid: missing hyphen
PRODABCD123-456XY   invalid: three digits after hyphen
PRODABCD123-4567X   invalid: one final letter
PRODABCD123-4567XYZ invalid: three final letters
```

## 11. Security, privacy, and performance controls

The implementation MUST:

- use only allowlisted built-in rule types;
- bound source-value and template lengths;
- avoid catastrophic-backtracking regex behavior;
- reject unsupported Unicode/control characters according to explicit policy;
- preserve leading zeros by treating identifiers as strings;
- protect manifest/workbook output from formula injection;
- redact raw values from logs and exceptions;
- apply field-level evidence masking;
- avoid connector calls inside rule or row loops;
- prohibit unbounded all-to-all fuzzy comparison;
- bound join cardinality, expansion ratios, token counts, lookup sizes, and
  candidate counts;
- require batched or staged reference and relationship resolution;
- version external reference data and fail closed when required evidence is
  unavailable or stale;
- aggregate passing outcomes to limit memory growth;
- preserve deterministic ordering;
- include rule configuration in canonical hashing;
- keep the Odoo connector read-only.

Rule evaluation is expected to be `O(source cells × rules per cell)`. The
initial implementation SHOULD place a conservative maximum on rules per source field
and record evaluation time and peak memory in the synthetic benchmark.

## 12. Rollout strategy

Recommended rollout:

1. implement and validate rules in fixture-only mode;
2. run the existing golden slice unchanged to prove unaffected behavior;
3. add a governed product-code pilot using `warn`;
4. review false positives and collision evidence with the data manager;
5. change the approved product-code rule to `reject`;
6. introduce low-risk `correct` rules for trim/collapse;
7. pilot sentence/title behavior only on selected descriptive fields;
8. expand to customer data with masking and stricter exception governance;
9. add exact dictionaries, scalar construction, and general validation rules;
10. add structural transforms with mandatory row-count reconciliation;
11. pilot domain validators on sanitized country-specific corpora;
12. introduce fuzzy entity candidates in suggestion-only mode;
13. add reviewed survivorship after false-positive/false-negative analysis;
14. require Odoo semantic and clean-package gates;
15. make controlled DEV/TEST rehearsal evidence mandatory before execution
    approval.

No rule should move directly from draft to rejection enforcement on an
unmeasured production-sized source package.

## 13. Decisions required before coding

The following business decisions must be recorded during Slice 0:

1. Are product-code letters strictly uppercase or merely case-insensitive?
2. May the engine uppercase product codes automatically, or must it reject
   lowercase codes?
3. Which whitespace characters are safe to correct automatically?
4. Which fields may use sentence/title correction?
5. Which fields contain personal or sensitive data, and what evidence policy
   applies?
6. Do any correction events require package review, or only warnings?
7. Who owns each rule and who approves a rule change?
8. What is the exception process and expiry policy?
9. How long are raw/corrected rule artifacts retained?
10. Which pipeline or future approval component enforces the package gate?
11. Which structural transforms are required for the first migration, and what
    cardinality and reconciliation policy applies to each?
12. Which domain validators are required, for which countries, and what does
    each validator claim to prove?
13. Which fuzzy matching methods, blocking keys, thresholds, languages, and
    review roles are permitted?
14. Which field-level survivorship rules are functionally acceptable?
15. What is the External ID strategy for create, update, and relationship
    imports?
16. Which Odoo fields require company, currency, unit, or language context?
17. Which warnings may be accepted, and which findings always block a clean
    package?
18. Which low-risk case family, if any, may be excluded from the product-level
    95% claim?

## 14. Definition of done

The feature is complete only when:

- contracts, implementation, examples, and documentation agree;
- all committed profiles follow the current shape;
- all governed source consumers use the same corrected value;
- duplicate detection occurs after correction;
- target comparison does not hide dirty target values;
- format validation is bounded and deterministic;
- rule evidence is complete, protected, and reconciled;
- the package quality gate cannot be confused with command success;
- the data manager can preview rule impact before publication;
- every reviewed rule change has a retained profile/ruleset hash;
- at least 23 of the 24 applicable transformation case families are verified
  and every mandatory family is verified;
- structural transforms preserve row lineage and reconcile counts and control
  totals;
- fuzzy candidates cannot merge records without reviewer evidence;
- every survivor field has explicit provenance;
- domain validators state their proof boundary and reference-data version;
- Odoo External ID, selection, company, currency, unit, language, default, and
  custom-constraint risks are resolved or fail closed;
- every source row is accounted for as a candidate, reference,
  quarantine/exclusion, or other governed terminal category;
- the exact clean-package candidate passes controlled Odoo 19 DEV/TEST
  rehearsal;
- mapping validity, rule-engine success, normalization approval,
  clean-package certification, and execution approval remain separate states;
- the golden and deployment acceptance slices pass;
- performance and privacy gates pass;
- no Odoo write surface exists.
