# Data-quality coverage specification

## Status and purpose

**Status:** Proposed product coverage specification. It is not a claim that
all listed behavior is implemented.

This document defines the case families Impodo must verify before claiming
broad readiness for governed CSV/XLSX-to-Odoo migrations. Delivery sequencing
belongs in the
[data-quality implementation plan](data-quality-rules-implementation-plan.md).

Mapping previews, semantic validation, normalization approval, clean-package
certification, and Odoo execution approval are separate capabilities.

## Coverage rule

Impodo tracks:

1. **Specified:** supported behavior and failure semantics are explicit.
2. **Implemented:** production code and governed authoring exist.
3. **Verified:** tests, business examples, evidence, and performance gates pass.

A family counts toward coverage only when all three levels pass and its user
documentation states the limits. `PARTIAL`, `DESIGNED`, `GAP`, and
`IMPLEMENTED FOUNDATION` do not count as verified.

```text
coverage = verified applicable families / applicable families * 100
```

A 95% product claim requires at least 23 of the 24 families below and every
mandatory family. An inapplicable family may be excluded only when recorded
in project scope before preparation and confirmed by the data manager.

The percentage measures capability breadth; it never permits dirty rows. Every
import-candidate row remains subject to all clean-package gates.

## Terms

| Term | Meaning |
| --- | --- |
| Normalization | Deterministic representation without changing approved business meaning |
| Transformation | Governed construction, reshaping, joining, splitting, deriving, or translating |
| Cleansing | Detection, correction, warning, rejection, or quarantine of quality problems |
| Canonical row | Staged row with consistent values, lineage, rules, and issues |
| Clean package | Frozen canonical package passing every mandatory release gate |

## Coverage catalogue

| ID | Case family | Mandatory | Current baseline |
| --- | --- | :---: | --- |
| `TC-01` | Source integrity and profiling | Yes | Implemented foundation |
| `TC-02` | Column and schema operations | No | Partial |
| `TC-03` | Row selection and quarantine | Yes | Designed |
| `TC-04` | Text and Unicode normalization | Yes | Partial |
| `TC-05` | Null, boolean, and type semantics | Yes | Partial |
| `TC-06` | Numbers, money, percentages, and units | Yes | Partial |
| `TC-07` | Dates, datetimes, timezones, and Excel date systems | Yes | Partial |
| `TC-08` | Format, value, and conditional constraints | Yes | Partial |
| `TC-09` | Dictionaries, lookups, and selection values | Yes | Partial |
| `TC-10` | Constants, defaults, coalesce, and conditions | Yes | Partial |
| `TC-11` | Split, extract, concatenate, and calculate | No | Designed |
| `TC-12` | Multi-table and shape transformations | No | Designed |
| `TC-13` | Exact duplicates and correction collisions | Yes | Implemented foundation |
| `TC-14` | Fuzzy entity resolution | Yes | Gap |
| `TC-15` | Survivorship and consolidation | Yes | Gap |
| `TC-16` | Domain-specific validation | No | Gap |
| `TC-17` | Relationships and hierarchy | Yes | Implemented foundation |
| `TC-18` | Odoo target semantics | Yes | Partial |
| `TC-19` | Cross-field and cross-row business rules | Yes | Designed |
| `TC-20` | Distribution and anomaly controls | No | Partial |
| `TC-21` | Evidence, privacy, and governance | Yes | Implemented foundation |
| `TC-22` | Exception correction and reprocessing | Yes | Partial |
| `TC-23` | Repeatability, bounded scale, and batched access | Yes | Partial |
| `TC-24` | Reconciliation and clean-package certification | Yes | Partial |

No family is `VERIFIED` merely because a bounded preview or configuration
validator exists. Verification requires full applicable-row execution and
evidence.

## Processing boundary

The complete pipeline must preserve this order:

```text
immutable source evidence
-> structural selection and transformation
-> governed normalization and derived values
-> typed validation and collision checks
-> entity and relationship resolution
-> cross-row and Odoo semantic validation
-> canonical staging and quarantine
-> read-only target preflight
-> clean-package certification
-> controlled Odoo target rehearsal
```

Identity-changing rules run before duplicate and relationship checks. Fuzzy
matching may propose candidates but must not silently replace exact
business-key matching or merge records.

## Clean-package release gates

A package may be labelled **clean for Odoo target rehearsal** only when:

- every source row is accounted for as a candidate, reference, quarantine, or
  governed exclusion;
- no blocking rule event, key collision, required relationship ambiguity, or
  unknown required lookup remains;
- every fuzzy candidate and survivorship decision is reviewed and traceable;
- quarantine/exclusion reasons, owners, and counts reconcile;
- row counts and declared business control totals reconcile;
- source, mapping, ruleset, schema, canonical data, target evidence, and
  package hashes are frozen and current;
- warnings and permitted exceptions are acknowledged by an authorized role;
- privacy and sensitive-data checks pass;
- the exact package passes controlled Odoo 19 target rehearsal.

A valid mapping submission or `PASS` from a rule engine is not by itself a
clean-package certificate.

