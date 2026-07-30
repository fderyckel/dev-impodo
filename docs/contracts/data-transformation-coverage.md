# Data transformation coverage contract

## 1. Purpose and status

This contract defines the transformation and data-quality case families that
Impodo must cover before the product may claim broad readiness for governed
CSV/XLSX-to-Odoo migrations.

**Status:** Proposed normative coverage contract. It is not a statement that
all listed behavior is implemented.

This catalogue complements the
[data-quality rules implementation plan](../data-quality-rules-implementation-plan.md)
and the [end-to-end product vision](../product-vision.md). The implementation
plan defines delivery. This contract defines what "covered" and "clean for
Odoo import" mean.

## 2. Scope

The product-level coverage claim applies to:

- tabular `.csv` and `.xlsx` migration sources;
- one or more source tables feeding Odoo 19 models;
- scalar, many2one, one2many-through-the-child, and many2many values;
- deterministic local preparation, canonical staging, target preflight,
  approval, and controlled DEV/TEST execution;
- ordinary master data, reference data, and transactional migration rows.

The following are outside the initial 95% claim unless a project explicitly
adds them:

- OCR and unstructured-document extraction;
- image, audio, video, or binary-content correction;
- arbitrary Python, SQL, spreadsheet formulas, callbacks, or Odoo methods;
- unrestricted regular expressions;
- probabilistic enrichment that cannot retain deterministic evidence;
- general-purpose enterprise master-data management;
- streaming change-data capture and real-time synchronization;
- geocoding, deliverability, or other paid external enrichment unless a
  separately approved provider is configured.

An excluded capability must be recorded in the project scope. Exclusion cannot
be used to hide a case that is present in the actual source package.

## 3. Terms

| Term | Meaning |
| --- | --- |
| Normalization | Deterministic representation of one value without changing its approved business meaning |
| Transformation | Governed construction, reshaping, joining, splitting, deriving, or translating of data |
| Cleansing | Detection, correction, warning, rejection, or quarantine of data-quality problems |
| Validation | A rule that produces evidence and a pass, warning, or blocking result |
| Entity resolution | Identification of records that may describe the same business entity |
| Survivorship | Approved selection or construction of the preferred values for one resolved entity |
| Canonical row | A staged row whose values, lineage, rules, and issues are represented consistently |
| Clean package | A frozen canonical package that passes every mandatory release gate in this contract |

## 4. How the 95% claim is measured

Impodo tracks three separate coverage numbers:

1. **Specified coverage:** normative semantics and failure behavior exist.
2. **Implemented coverage:** production code and governed authoring exist.
3. **Verified coverage:** automated tests, business examples, evidence, and
   performance gates pass.

A case family counts as covered only when all five conditions are true:

- its supported and unsupported behavior is explicit;
- unsafe or ambiguous input fails closed;
- the browser or strict profile can author only valid rule shapes;
- deterministic tests cover pass, correction, warning, rejection, collision,
  and evidence behavior where applicable;
- the operator documentation explains the result and its limits.

`PARTIAL`, `DESIGNED`, and `GAP` do not count as covered.

For the 24 common case families in this contract:

```text
coverage percentage = VERIFIED applicable families / applicable families × 100
```

A product-wide 95% claim requires at least 23 of 24 families to be `VERIFIED`.
Every high-risk family marked **mandatory** must be verified regardless of the
percentage. A project-specific claim may exclude an inapplicable family only
when the exclusion is recorded before source preparation and confirmed by the
data manager.

The 95% figure measures capability breadth. It never permits 5% dirty rows.
Every import-candidate row remains subject to the 100% clean-package gates.

### 4.1 Data-quality dimensions and proof boundary

The catalogue must be evaluated across the usual data-quality dimensions:

| Dimension | Required proof |
| --- | --- |
| Completeness | Required values, expected datasets, row populations, and relationships are present or explicitly quarantined |
| Validity and conformity | Values satisfy declared type, format, range, dictionary, and Odoo field rules |
| Uniqueness | Governed business keys and External IDs remain unique after every correction and consolidation |
| Consistency | Cross-field, cross-row, cross-dataset, currency, unit, company, and temporal rules agree |
| Referential integrity | Every required relationship resolves exactly once in the intended scope |
| Timeliness | Export cutoff, reference-data version, target snapshot age, and permitted staleness are explicit |
| Accuracy | Values agree with an identified authoritative source, approved reference set, or accountable business decision |
| Reasonableness | Distributions, totals, trends, and outliers remain within approved expectations or are reviewed |

A syntactically valid value is not necessarily accurate. Impodo must not claim
that an email exists, an address is deliverable, a name is legally correct, or
a price is commercially correct unless the package contains appropriate
authoritative evidence. Where accuracy or timeliness cannot be automated, the
clean-package certificate must retain the accountable human decision and its
evidence.

### 4.2 Required coverage evidence

The product coverage register must record, for each applicable `TC-*` family:

- product owner and risk classification;
- exact supported and unsupported cases;
- implementation status and owning component;
- browser/profile authoring route;
- semantic and evidence contract versions;
- automated test and sanitized acceptance-corpus references;
- representative-volume performance result;
- operator-documentation link;
- verification date, verifier, and any approved exclusion.

A narrative statement such as "supports cleansing" is not coverage evidence.

## 5. Current status vocabulary

| Status | Meaning |
| --- | --- |
| `IMPLEMENTED FOUNDATION` | A useful current component exists, but this contract's complete family gate is not yet satisfied |
| `PARTIAL` | Some rules or contracts exist; material semantics or runtime behavior remain |
| `DESIGNED` | Product documentation describes the direction but implementation-level semantics remain incomplete |
| `GAP` | The family was absent or materially under-specified before this catalogue |
| `VERIFIED` | All coverage conditions in section 4 pass |

No current family should be inferred as `VERIFIED` merely because a preview or
mapping validator exists. Bounded sample previews are not full-row execution.

## 6. Coverage catalogue

| ID | Case family | Minimum required behavior | Risk | Current baseline |
| --- | --- | --- | --- | --- |
| `TC-01` | Source integrity and profiling | Hash verification, encoding/delimiter/header control, bounded previews, types, nulls, distinct counts, ranges, formulas, and structural warnings | **Mandatory** | Implemented foundation |
| `TC-02` | Column and schema operations | Select, rename, copy, drop, reorder, and type columns without losing source lineage | Standard | Partial |
| `TC-03` | Row selection and quarantine | Keep, reject, or quarantine rows through explicit predicates; retain source-row identity and exclusion reasons | **Mandatory** | Designed |
| `TC-04` | Text and Unicode normalization | Trim, collapse, line endings, character width, Unicode form, casing, punctuation, diacritics policy, and forbidden/invisible characters | **Mandatory** | Partial |
| `TC-05` | Null, boolean, and type semantics | Distinguish null, empty, whitespace, zero, false, and sentinel text; strict integer/decimal/boolean/date/datetime parsing | **Mandatory** | Partial |
| `TC-06` | Numbers, money, percentages, and units | Declared locale, grouping, sign, scientific notation, precision, rounding, currency, percentage, and unit-of-measure conversion | **Mandatory** | Partial |
| `TC-07` | Dates and datetimes | Declared date order, Excel date system, timezone, daylight-saving ambiguity, invalid dates, date arithmetic, and canonical UTC | **Mandatory** | Partial |
| `TC-08` | Format and value constraints | Length, range, allowlist, denylist, bounded patterns, checksums, requiredness, and conditional requiredness | **Mandatory** | Partial |
| `TC-09` | Dictionaries, lookups, and selections | Versioned exact translations, synonyms, Odoo selection keys, unknown/ambiguous handling, and lookup lineage | **Mandatory** | Partial |
| `TC-10` | Constants, defaults, coalesce, and conditions | Constant, source fallback, leave-unset/Odoo-default, ordered coalesce, and bounded if/then/else without silent overwrite | **Mandatory** | Partial |
| `TC-11` | Split, extract, concatenate, and calculate | Separator, quoting, escaping, token order, empty tokens, substrings, bounded extraction, arithmetic, and derived-field lineage | Standard | Designed |
| `TC-12` | Multi-table and shape transformations | Join, append/union, pivot, unpivot, group, aggregate, expand one row, combine rows, and conflict policy | Standard | Designed |
| `TC-13` | Exact duplicates and correction collisions | Duplicate source rows, scoped key duplicates, post-correction collisions, many2many duplicates, and deterministic blocking | **Mandatory** | Implemented foundation |
| `TC-14` | Fuzzy entity resolution | Candidate generation, blocking keys, similarity method/version, score, thresholds, alternatives, multilingual limits, and mandatory human judgment | **Mandatory** | Gap |
| `TC-15` | Survivorship and consolidation | Source priority, recency, completeness, field-level choice, conflicts, golden-record lineage, and no silent merge | **Mandatory** | Gap |
| `TC-16` | Domain-specific validation | Email, phone, country/subdivision/postcode, address, VAT/tax, IBAN/BIC, GTIN/EAN, URL, and organization-approved domain rules | Standard | Gap |
| `TC-17` | Relationships and hierarchy | Incoming/target lookup, scoped keys, missing/ambiguous references, cycles, parent/child consistency, and dependency order | **Mandatory** | Implemented foundation |
| `TC-18` | Odoo target semantics | External IDs, selection technical values, company-dependent fields, check-company behavior, currency/UoM context, translations, archive state, readonly/computed/custom constraints | **Mandatory** | Partial |
| `TC-19` | Cross-field and cross-row business rules | Conditional completeness, date order, totals, balances, mutually exclusive values, parent/child totals, and project-specific declarative rules | **Mandatory** | Designed |
| `TC-20` | Distribution and anomaly controls | Frequencies, outliers, unexpected drift, value-distribution changes, threshold policy, and review evidence | Standard | Partial |
| `TC-21` | Evidence, privacy, and governance | Raw/governed/typed lineage, masking, rule ownership/version, correction decisions, hashes, approval, and immutable history | **Mandatory** | Implemented foundation |
| `TC-22` | Exception correction and reprocessing | Quarantine queue, reason, owner, expiry, correction route, rerun, resolved evidence, and no in-place mutation of frozen input | **Mandatory** | Partial |
| `TC-23` | Repeatability and scale | Idempotence, deterministic ordering, bounded resources, safe pattern runtime, batched lookups, no per-row Odoo calls, and representative-scale proof | **Mandatory** | Partial |
| `TC-24` | Reconciliation and clean-package certification | Source/staged/candidate/quarantine counts, control totals, target changes, package hash, staleness, DEV/TEST rehearsal, and final data-manager decision | **Mandatory** | Partial |

## 7. Fixed processing order

The complete transformation pipeline must preserve this order:

```text
immutable source bytes and source-row identity
-> parsing and structural table selection
-> column/row structural transformations
-> governed source-cell normalization
-> constants, lookups, derived values, and table-shape transformations
-> technical type parsing
-> field and conditional validation
-> exact duplicate and post-correction collision checks
-> fuzzy entity candidates and survivorship decisions
-> relationship and hierarchy resolution
-> cross-field, cross-row, and distribution controls
-> Odoo semantic validation
-> canonical staging and quarantine
-> read-only target preflight
-> clean-package certification
-> controlled DEV/TEST rehearsal
```

Rules that alter identity must run before duplicate and relationship checks.
Fuzzy matching must consume governed values but must not replace exact
business-key matching. Odoo semantic validation must consume canonical values,
not raw cells.

## 8. Requirements for the previously missing families

### 8.1 Structural and derived transformations

Every structural transform must declare:

- input datasets and exact source hashes;
- join keys, join kind, cardinality expectation, and unmatched-row policy;
- output row trace identity;
- duplicate-column and name-conflict behavior;
- grouping keys, aggregate functions, null behavior, and rounding;
- expansion or combination trace suffixes;
- expected row-count equation and reconciliation result.

A many-to-many join that unexpectedly multiplies rows must block. A transform
must never silently drop unmatched rows or select one of several matches.

### 8.2 Entity resolution and survivorship

Fuzzy matching is a review aid, not an automatic correction. It must provide:

- deterministic candidate generation using versioned algorithms;
- blocking keys that avoid all-to-all comparison at scale;
- per-field comparison methods and weights;
- similarity scores and all candidates above the review threshold;
- explicit `MATCH`, `NOT_A_MATCH`, or `DEFER` decisions;
- multilingual and character-set limitations;
- retained raw records and immutable reviewer evidence.

Survivorship is a separate decision. Each output field must identify whether
its value came from a named source row, an approved deterministic rule, or an
explicit reviewer choice. No "golden record" may lose conflicting values
without evidence.

### 8.3 Domain-specific validators

Domain validators must be allowlisted, versioned, and project-selected.
Initial candidates are:

- email structure and domain normalization without claiming mailbox
  deliverability;
- phone parsing with an explicit country/region and canonical E.164 output;
- country and subdivision codes with postcode consistency;
- VAT/tax identifiers and organization-approved checksum rules;
- IBAN/BIC checksum and country/length consistency;
- GTIN/EAN checksum and significant-leading-zero preservation;
- URL scheme/host validation;
- optional postal-address verification through licensed, versioned reference
  data.

Failure to reach an external verification service must not silently convert an
unknown result into valid data.

### 8.4 Odoo semantic certification

Before a row is import-candidate:

- its External ID strategy must be explicit, unique, stable, and portable;
- relationship values must use External IDs or governed business keys, never
  remembered numeric database IDs;
- source labels for selection fields must translate to captured technical
  selection keys;
- company-dependent values and company-scoped relationships must be evaluated
  in the intended company;
- monetary values must be paired with currency and rounding context;
- quantities must be paired with the intended unit of measure and conversion
  rule;
- translated fields must declare language;
- fields intended to use an Odoo runtime default must remain visibly
  unverified until DEV/TEST rehearsal;
- readonly, computed, related, inverse, custom constraints, and model-specific
  behavior must fail closed or be explicitly deferred to rehearsal.

Read-only validation cannot prove every Odoo ORM constraint, automation,
onchange, compute, or custom module rule. DEV/TEST execution evidence remains
mandatory.

## 9. Clean-package release gates

The data manager may label a package **clean for Odoo DEV/TEST rehearsal** only
when all applicable gates pass:

- 100% of source rows are accounted for as canonical candidates, references,
  or quarantined/excluded rows;
- zero unresolved blocking rule events;
- zero missing or duplicate post-normalization business keys;
- zero unresolved correction collisions;
- zero ambiguous required relationships;
- zero unknown required lookup or selection values;
- zero unreviewed fuzzy duplicate candidates;
- every survivorship decision is complete and traceable;
- every quarantine/exclusion has a reason, owner, and reconciled count;
- row counts and declared financial, quantity, and other control totals
  reconcile;
- source, mapping, ruleset, schema, canonical data, target evidence, and
  package hashes are frozen;
- no source, mapping, rule, schema, or target staleness is present;
- every warning and permitted exception is acknowledged by the authorized
  role;
- the package contains no unresolved sensitive-data exposure;
- the exact package passes controlled Odoo 19 DEV/TEST rehearsal.

`PASS` from a rules engine is not enough when any gate above is absent.
Likewise, a valid mapping submission is not a clean-package certificate.

## 10. Minimum acceptance packs

Verification must include sanitized corpora for:

- multilingual Unicode, whitespace, punctuation, accents, width, and case;
- locale numbers, currencies, percentages, units, and rounding boundaries;
- Excel 1900/1904 dates, leap dates, timezone offsets, and daylight-saving
  ambiguity;
- null, empty, zero, false, sentinel text, and invalid tokens;
- identifiers with leading zeros, punctuation, checksums, and scientific
  notation hazards;
- exact duplicates, post-correction collisions, fuzzy candidates, false
  positives, false negatives, and survivor conflicts;
- email, phone, address, VAT/tax, IBAN/BIC, GTIN/EAN, and URL examples;
- one-to-one, one-to-many, many-to-one, and many-to-many source shapes;
- join multiplication, unmatched joins, pivot/unpivot, aggregation, expansion,
  and row-count reconciliation;
- Odoo External IDs, selections, companies, currencies, units, translations,
  defaults, archived rows, and custom constraints;
- full, masked, and suppressed evidence;
- representative historical volume without connector calls inside row loops.

## 11. Landscape traceability

This catalogue was checked against official documentation for:

- [OpenRefine transformations](https://openrefine.org/docs/manual/transforming)
  and [reconciliation](https://openrefine.org/docs/manual/reconciling);
- [Microsoft Power Query transformations](https://learn.microsoft.com/en-us/power-query/power-query-what-is-power-query)
  and [fuzzy merge](https://learn.microsoft.com/en-us/power-query/merge-queries-fuzzy-match);
- [Informatica Cleanse transformation](https://docs.informatica.com/integration-cloud/data-integration/current-version/transformations/cleanse-transformation.html)
  and [deduplication/consolidation](https://docs.informatica.com/integration-cloud/data-integration/current-version/transformations/deduplicate-transformation/deduplication-and-consolidation-operations.html);
- [Talend Data Preparation functions](https://help.qlik.com/talend/en-US/data-preparation-user-guide/7.3/list-of-functions)
  and [Data Quality components](https://help.qlik.com/talend/en-US/components/8.0/data-quality-components-container);
- [Alteryx preparation tools](https://help.alteryx.com/current/en/designer/tools/preparation.html)
  and [fuzzy matching](https://help.alteryx.com/aac/en/designer-experience/designer-cloud-tool-list/standard-mode-tools/workflow-join-tools/fuzzy-match-tool.html);
- [AWS Glue DataBrew quality rules](https://docs.aws.amazon.com/databrew/latest/dg/profile.data-quality-rules.html);
- [Odoo 19 import guidance](https://www.odoo.com/documentation/19.0/applications/essentials/export_import_data.html)
  and [multi-company guidance](https://www.odoo.com/documentation/19.0/developer/howtos/company.html).

Competitor behavior is evidence for case discovery, not an authorization to
copy unsafe semantics. Impodo retains its stricter allowlist, evidence,
approval, read-only preflight, and Odoo-specific safety boundaries.
