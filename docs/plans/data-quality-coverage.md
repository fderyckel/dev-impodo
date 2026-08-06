# Data-quality coverage ledger

## Status and purpose

**Status:** Active planning and acceptance ledger. Durable staging, integrated
quality checks, and quarantine are implemented; Impodo does not yet claim to
produce a certified clean migration package.

This document is the single authority for data-quality capability breadth and
clean-package gates. Delivery order belongs in the
[data-quality and staging plan](data-quality-and-staging-plan.md); the product
workflow belongs in the [product vision](../product-vision.md).

The implemented structural, reference, domain, anomaly, fuzzy-resolution,
survivorship, and governed-correction foundation is recorded in the
[Slice 6 advanced coverage plan](slice-6-advanced-coverage-plan.md). The
baseline remains conservative wherever target rehearsal or package-level proof
is still absent.

The browser now integrates full-row staging, quality, quarantine, and eligible
row filtering before the existing read-only Odoo comparison. Normalization
approval, clean-package certification, and Odoo execution are still separate
or absent, so Impodo makes no product-wide clean-package readiness claim today.

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
| `TC-03` | Row selection and quarantine | Yes | `VERIFIED` | Broader inline correction UX belongs to normalization review |
| `TC-04` | Text and Unicode normalization | Yes | `PARTIAL` | Runtime parity across all rows and multilingual evidence |
| `TC-05` | Null, boolean, and type semantics | Yes | `PARTIAL` | Integrated staging execution and edge-case acceptance |
| `TC-06` | Numbers, money, percentages, and units | Yes | `PARTIAL` | Currency, unit, locale, precision, and rounding context |
| `TC-07` | Dates, datetimes, timezones, and Excel date systems | Yes | `PARTIAL` | Full date-system and timezone execution evidence |
| `TC-08` | Format, value, and conditional constraints | Yes | `PARTIAL` | Bounded full-row validators and conditional rules |
| `TC-09` | Dictionaries, lookups, and selection values | Yes | `PARTIAL` | Exact versioned translations and captured technical-key checks are integrated; rehearsal recheck remains |
| `TC-10` | Constants, defaults, fallbacks, and conditions | Yes | `PARTIAL` | Full-row execution and Odoo-default rehearsal evidence |
| `TC-11` | Split, extract, concatenate, and calculate | Scope | `PARTIAL` | Compile bounded authoring plans into canonical rows |
| `TC-12` | Multi-table and shape transformations | Scope | `PARTIAL` | Exact joins, union-all, grouping, lineage, and reconciliation are integrated; broader shapes remain out of scope |
| `TC-13` | Exact duplicates and correction collisions | Yes | `VERIFIED` | Extend fixtures when new identity-changing transforms are added |
| `TC-14` | Fuzzy entity resolution | Yes | `PARTIAL` | Bounded deterministic candidates and explicit accept/reject evidence are integrated; broader business fixtures remain |
| `TC-15` | Survivorship and consolidation | Yes | `PARTIAL` | Reviewed survivor decisions and field-level provenance are integrated; broader relationship cases remain |
| `TC-16` | Domain-specific validation | Scope | `PARTIAL` | Allowlisted checksum, IBAN, postal, date-window, and approved-code checks are integrated; scope-specific proof remains |
| `TC-17` | Relationships and hierarchy | Yes | `PARTIAL` | Incoming dependency quarantine is integrated; broader hierarchy cases remain |
| `TC-18` | Odoo target semantics | Yes | `PARTIAL` | Company, currency, UoM, defaults, constraints, and rehearsal |
| `TC-19` | Cross-field and cross-row business rules | Yes | `PARTIAL` | Guided rules, collisions, joins, and aggregates are integrated; package proof remains |
| `TC-20` | Distribution and anomaly controls | Scope | `PARTIAL` | Governed metric boundaries and IQR evidence are integrated; full-package review remains |
| `TC-21` | Evidence, privacy, and governance | Yes | `PARTIAL` | Quality retention and hidden technical evidence are integrated; package approval remains |
| `TC-22` | Exception correction and reprocessing | Yes | `PARTIAL` | Typed governed corrections, provenance, immutable reruns, and invalidation are integrated; package certification remains |
| `TC-23` | Repeatability, bounded scale, and batched access | Yes | `PARTIAL` | The 25,000-row advanced browser flow is measured; production and Odoo transport sizing remain |
| `TC-24` | Reconciliation and clean-package certification | Yes | `PARTIAL` | Dual row accounting is integrated; certificate and target rehearsal remain |

`Scope` means mandatory when the project uses that capability. The project
must not silently classify a required family as inapplicable.

## Clean-package gates

A package may be labelled **clean for Odoo target rehearsal** only when:

- every physical source row has exactly one accounting entry and every
  canonical row has exactly one reconciled disposition: import candidate,
  reference, blocked, quarantine, or governed exclusion;
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
