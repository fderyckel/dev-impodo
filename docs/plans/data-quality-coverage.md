# Data-quality coverage ledger

## Status and purpose

**Status:** Active planning and acceptance ledger. It does not claim that
Impodo currently produces a clean migration package.

This document is the single authority for data-quality capability breadth and
clean-package gates. Delivery order belongs in the
[data-quality and staging plan](data-quality-and-staging-plan.md); the product
workflow belongs in the [product vision](../product-vision.md).

The current browser and expert CLI provide useful but separate foundations.
There is no integrated full-row staging, quarantine, certification, or Odoo
execution workflow, so Impodo makes no product-wide clean-package readiness
claim today.

## Status meanings

| Status | Meaning |
| --- | --- |
| `VERIFIED` | Integrated behavior, tests, business examples, retained evidence, performance, and user guidance all pass |
| `FOUNDATION` | Useful implemented capability exists, but not in the complete staging-to-package flow |
| `PARTIAL` | Some cases are implemented; material cases or integration remain |
| `DESIGNED` | Required behavior is understood but not executed by the product |
| `GAP` | No sufficient product implementation or accepted design exists |

A preview, mapping validator, isolated domain object, or CLI fixture is never
enough by itself to mark a family `VERIFIED`.

## Release rule

A migration scope may claim coverage only when:

- every applicable mandatory family is `VERIFIED`;
- every additional family declared in that project scope is `VERIFIED`;
- any inapplicable family is recorded before preparation and confirmed by the
  data manager;
- every clean-package gate below passes for the exact frozen package.

There is no percentage shortcut. Capability breadth cannot permit a dirty,
unaccounted, ambiguous, or unapproved row.

## Coverage catalogue

| ID | Case family | Mandatory | Current baseline | Main remaining proof |
| --- | --- | :---: | --- | --- |
| `TC-01` | Source integrity and profiling | Yes | `FOUNDATION` | Execute against every staged row and retain package evidence |
| `TC-02` | Column and schema operations | Scope | `PARTIAL` | Full-row structural compiler and reconciliation |
| `TC-03` | Row selection and quarantine | Yes | `DESIGNED` | Durable dispositions, ownership, correction, and rerun |
| `TC-04` | Text and Unicode normalization | Yes | `PARTIAL` | Runtime parity across all rows and multilingual evidence |
| `TC-05` | Null, boolean, and type semantics | Yes | `PARTIAL` | Integrated staging execution and edge-case acceptance |
| `TC-06` | Numbers, money, percentages, and units | Yes | `PARTIAL` | Currency, unit, locale, precision, and rounding context |
| `TC-07` | Dates, datetimes, timezones, and Excel date systems | Yes | `PARTIAL` | Full date-system and timezone execution evidence |
| `TC-08` | Format, value, and conditional constraints | Yes | `PARTIAL` | Bounded full-row validators and conditional rules |
| `TC-09` | Dictionaries, lookups, and selection values | Yes | `PARTIAL` | Versioned translations and Odoo technical-key validation |
| `TC-10` | Constants, defaults, fallbacks, and conditions | Yes | `PARTIAL` | Full-row execution and Odoo-default rehearsal evidence |
| `TC-11` | Split, extract, concatenate, and calculate | Scope | `PARTIAL` | Compile bounded authoring plans into canonical rows |
| `TC-12` | Multi-table and shape transformations | Scope | `PARTIAL` | Joins, unions, grouping, cardinality, and control totals |
| `TC-13` | Exact duplicates and correction collisions | Yes | `FOUNDATION` | Detect after every identity-changing transformation |
| `TC-14` | Fuzzy entity resolution | Yes | `GAP` | Bounded candidates, review decisions, and false-match evidence |
| `TC-15` | Survivorship and consolidation | Yes | `GAP` | Field-level provenance and approved survivor decisions |
| `TC-16` | Domain-specific validation | Scope | `GAP` | Versioned validators with explicit proof boundaries |
| `TC-17` | Relationships and hierarchy | Yes | `FOUNDATION` | Integrated full-row resolution and target evidence |
| `TC-18` | Odoo target semantics | Yes | `PARTIAL` | Company, currency, UoM, defaults, constraints, and rehearsal |
| `TC-19` | Cross-field and cross-row business rules | Yes | `DESIGNED` | Deterministic evaluator and reconciled evidence |
| `TC-20` | Distribution and anomaly controls | Scope | `PARTIAL` | Governed thresholds and full-package review evidence |
| `TC-21` | Evidence, privacy, and governance | Yes | `FOUNDATION` | Integrated masking, retention, approval, and package lineage |
| `TC-22` | Exception correction and reprocessing | Yes | `DESIGNED` | Immutable corrections, expiry, ownership, and rerun lifecycle |
| `TC-23` | Repeatability, bounded scale, and batched access | Yes | `PARTIAL` | Historical-scale runtime, memory, and transport evidence |
| `TC-24` | Reconciliation and clean-package certification | Yes | `GAP` | Integrated row accounting, certificate, and target rehearsal |

`Scope` means mandatory when the project uses that capability. The project
must not silently classify a required family as inapplicable.

## Clean-package gates

A package may be labelled **clean for Odoo target rehearsal** only when:

- every source row has exactly one reconciled disposition: import candidate,
  reference, quarantine, or governed exclusion;
- no blocking issue, identity collision, required relationship ambiguity, or
  unknown required lookup remains;
- every fuzzy candidate, survivor choice, correction, warning, and exception
  has the required review evidence;
- row counts and declared business control totals reconcile through every
  transformation;
- source, selection, derived-entity plan, mapping, rules, schema, canonical
  data, target snapshot, and package hashes are frozen and current;
- privacy, masking, access, and retention controls pass;
- all applicable catalogue families satisfy the release rule;
- the exact package passes an authorized Odoo 19 target rehearsal.

A valid mapping submission, successful rule evaluation, approved
normalization dry run, or successful preflight classification is not by itself
a clean-package certificate or Odoo write authorization.
