# Data-quality and staging delivery plan

## Status and ownership

**Status:** Active plan for the next integrated delivery. The complete flow is
not implemented.

This plan owns the work between a submitted browser mapping and a certifiable
read-only preflight package. It does not redefine:

- source, schema, and mapping behavior in the
  [browser workspace contract](../contracts/workspace.md);
- the expert CLI matcher and classifier in the
  [preflight contract](../contracts/preflight.md);
- the standalone approval lifecycle in the
  [normalization governance contract](../contracts/normalization-governance.md);
- product stages or later Odoo execution in the
  [product vision](../product-vision.md);
- case-family readiness and clean-package gates in the
  [coverage ledger](data-quality-coverage.md).

## Current reality

| Capability | Current state |
| --- | --- |
| Source evidence | Browser registration, inspection, confirmation, and frozen dataset selections are integrated and hash-bound |
| Target schema | Read-only Odoo 19 capture, permitted model scope, business keys, and manual-draft boundary are integrated |
| Mapping | Immutable revisions, scalar providers, allowlisted transformations, relationships, validation, and exact-hash submission are integrated |
| Derived entities | Lookup and parent/child preparation rules can be authored and previewed, but do not yet produce durable full-row datasets |
| Normalization governance | Immutable dry-run decisions and freeze rules exist as standalone domain behavior, not as a browser or repository workflow |
| Read-only preflight | The profile-driven CLI can snapshot, resolve, compare, and classify, but it does not consume submitted browser mappings |
| Export approval | Frozen-plan approval objects exist as standalone domain behavior, without an integrated staged package or executor |
| Staging and certification | Durable canonical rows, quarantine, integrated full-row quality execution, and clean-package certification are absent |

The missing product seam is therefore not another mapping editor or another
rule language. It is deterministic full-row execution and evidence connecting
the implemented components.

## Target flow

```text
frozen source datasets
-> submitted mapping + derived-entity plan
-> full-row semantic evaluation
-> canonical staging + quarantine
-> normalization review and freeze
-> batched read-only Odoo preflight
-> clean-package certification
```

The profile-driven CLI remains an expert input. Where its semantics overlap
with browser mappings, both paths must reuse the same canonical types,
relationship policies, comparison rules, and evidence vocabulary rather than
drifting independently.

## Delivery principles

- Reuse `mapping_semantics.py` and the derived-entity contract; do not create a
  second transformation language whose preview and runtime meanings can
  diverge.
- Preserve registered source bytes. Corrections create governed proposed
  values and evidence, never silent source-file edits.
- Bind every run to exact source-selection, derived-plan, mapping, schema,
  evaluator, ruleset, and target-evidence hashes.
- Give every source row a stable trace identity and one terminal accounting
  disposition.
- Keep portable evidence free of numeric Odoo record IDs. Resolve and report
  through governed business keys and scope.
- Stream or batch full-row work with bounded memory. Odoo metadata and records
  are requested per model and in bounded pages, never once per source row.
- Fail closed when company, currency, unit, language, selection, default,
  computed-field, or custom-constraint context is required but unproven.
- Keep mapping submission, normalization approval, clean-package
  certification, execution approval, and execution as separate states.

## Delivery slices

### Slice 0 — Lock the integration contract

Define the versioned staging-run envelope, canonical row, lineage, issue,
quarantine, reconciliation, and package-manifest contracts. Record invalidation
rules for every bound input and decide how browser mappings and expert profiles
lower into shared semantics.

**Gate:** the contracts identify one authority for every value, status, hash,
and transition; no preview-only behavior is described as full-row execution.

### Slice 1 — Execute current authoring semantics over every row

Compile the exact submitted mapping and current derived-entity plan. Execute
existing providers, fallbacks, null policies, scalar transformations, types,
and bounded related-dataset rules over frozen source data. Emit proposed typed
values, issues, and lineage without connecting to Odoo.

**Gate:** preview and runtime produce the same result for the same value, and
unsupported semantics block rather than fall back silently.

### Slice 2 — Persist canonical staging and reconciliation

Add durable project-scoped staging in DuckDB with atomic run publication.
Retain raw source pointers, governed values, typed proposed values, lineage,
issues, and deterministic ordering. Process large inputs in bounded batches
and record row-count equations and business control totals.

**Gate:** unchanged inputs produce identical portable evidence; every
transformation explains any created, combined, excluded, or quarantined row.

### Slice 3 — Add quality rules and quarantine

Execute the first applicable families from the
[coverage ledger](data-quality-coverage.md): post-transformation identity
collisions, required values, bounded formats, lookups, cross-field rules, and
relationship readiness. Add immutable quarantine reasons, ownership, expiry,
correction evidence, and rerun behavior.

**Gate:** every source row reaches exactly one reconciled disposition, with no
silent drops, guessed lookups, or unresolved required relationships.

### Slice 4 — Integrate normalization review

Build the existing dry-run summary from staged evidence, persist immutable
group decisions and whole-run approval, and freeze the exact canonical dataset
hash. Any changed input creates a new run and invalidates approval eligibility.

**Gate:** required correction groups and collisions cannot be bypassed, and a
normalization approval grants no Odoo capability.

### Slice 5 — Integrate read-only preflight

Adapt frozen canonical rows to the existing prepared-record and preflight
contracts. Plan metadata once per model, merge record requirements, split
large domains into bounded requests, and build indexed relationship lookups.
Retain target fingerprints and snapshot hashes with the staged run.

**Gate:** no connector call occurs inside a row loop; equivalent browser and
profile fixtures produce equivalent portable identities, resolutions, and
classifications.

### Slice 6 — Close advanced coverage gaps

Add only the families required by approved migration scopes: structural joins
and aggregations, versioned reference data, domain validators, anomaly rules,
fuzzy candidate generation, reviewer decisions, and field-level survivorship.
Each extension must preserve bounded execution, provenance, and reconciliation.

**Gate:** no join multiplies rows unexpectedly, no fuzzy candidate is merged
automatically, and every survivor value records its source and decision.

### Slice 7 — Certify a clean package and rehearse

Evaluate every applicable clean-package gate, freeze the complete package
manifest, and bind any approval to that exact content. Validate Odoo selection
technical keys, External ID strategy, company boundaries, currencies, units,
languages, readonly/computed fields, defaults, and deferred custom constraints.

Target rehearsal requires a separately authorized Odoo 19 environment and
adapter. Production execution remains outside this plan.

**Gate:** the exact package passes the coverage ledger and authorized target
rehearsal; any changed input invalidates the certificate and approval.

## Acceptance requirements

The integrated delivery is complete only when:

- all current browser workflows and expert preflight behavior remain valid;
- full-row execution matches preview semantics for supported rules;
- every source and constructed row has deterministic lineage and disposition;
- corrections, warnings, errors, quarantine, and approvals reconcile;
- required relationship resolution uses governed keys and scope;
- target access stays read-only and batched through the preflight boundary;
- portable artifacts contain no credentials or numeric Odoo IDs;
- fixed inputs produce deterministic manifests;
- historical-scale runtime, memory, snapshot size, and workbook size meet
  recorded limits;
- all applicable coverage families and clean-package gates pass;
- documentation continues to distinguish review evidence from Odoo write
  authorization.

Detailed acceptance cases and commands belong in
[testing/acceptance.md](../testing/acceptance.md), not in this plan.

## Decisions required before Slice 0 closes

1. Which data-quality families and structural transforms are required for the
   first integrated migration scope?
2. Which corrections may run automatically, which require review, and who owns
   each policy?
3. What are the quarantine ownership, expiry, correction, and retention rules?
4. How will browser mappings and expert profiles share or translate semantics
   without two competing authorities?
5. Which fields require masked or suppressed row-level evidence?
6. What External ID strategy will the future executor consume?
7. Which Odoo 19 database is authorized for target rehearsal, and what evidence
   proves that it is isolated from production?
8. What source size, runtime, memory, and report-size limits define acceptance?

## Out of scope

- modifying registered source files;
- arbitrary in-process Python, server actions, unbounded regular expressions,
  or formulas with file, network, environment, import, loop, or Odoo access;
- automatic fuzzy merges or unreviewed survivorship;
- generic Odoo RPC, SQL, or production write capability;
- treating a successful command, mapping submission, or approval as an
  executable import authorization.
