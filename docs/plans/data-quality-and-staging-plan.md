# Data-quality and staging delivery plan

## Status and ownership

**Status:** Active plan for the next integrated delivery. The complete flow is
not implemented.

This plan owns the work that turns the implemented, transient all-row browser
readiness path into durable canonical staging, governed normalization, and a
certifiable read-only preflight package. It does not redefine:

- source, schema, and mapping behavior in the
  [browser workspace contract](../contracts/workspace.md);
- the expert CLI matcher and classifier in the
  [preflight contract](../contracts/preflight.md);
- the durable canonical evaluation foundation in the
  [canonical staging contract](../contracts/canonical-staging.md);
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
| Derived entities | Lookup and parent/child preparation rules are authored in the browser, repeated over every frozen source row, and published as durable canonical datasets with complete physical-row pointers |
| Normalization governance | Immutable dry-run decisions and freeze rules exist as standalone domain behavior, not as a browser or repository workflow |
| Canonical evaluation | Exact submitted browser mappings use a reusable storage- and Odoo-independent full-row evaluator. Server previews and runtime share one scalar boundary; the current in-memory path is explicitly limited to 100,000 physical rows |
| Read-only preflight | Strict CLI profiles and exact submitted browser mapping revisions both feed the preflight engine. Browser readiness publishes canonical evidence, batches target reads, classifies results, and persists the report and technical manifest |
| Export approval | Frozen-plan approval objects exist as standalone domain behavior, without an integrated staged package or executor |
| Staging and certification | Durable atomic canonical staging, row controls, and opt-in named business totals are integrated. Quarantine, integrated quality execution, and clean-package certification remain absent |

The next missing product seam is therefore not another mapping editor,
transformation language, or browser-to-preflight adapter. It is governed
full-row quality evaluation and quarantine over durable staging, followed by
normalization review, clean-package certification, and direct reuse of frozen
evidence by preflight.

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
- Give every physical source row one traceable accounting entry and every
  canonical row one terminal disposition; retain explicit fan-out links.
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

**Checkpoint:** the versioned in-memory staging run, canonical row, lineage,
issue, source-side disposition, reconciliation, and deterministic content-hash
contracts are implemented. Slice 0 remains open for durable publication and
invalidation transitions, quarantine ownership/evidence, and the complete
package-manifest boundary.

### Slice 1 — Extract and reuse the current all-row evaluation

Refactor the implemented browser-readiness preparation into a reusable,
storage-independent evaluator. It continues to compile the exact submitted
mapping and current derived-entity plan and to execute existing providers,
fallbacks, null policies, scalar transformations, types, and bounded
related-dataset rules over every frozen source row. Emit proposed typed values,
issues, and lineage without requiring Odoo access; the current transient
readiness behavior remains the compatibility path while durable publication is
added in Slice 2.

**Gate:** preview and runtime produce the same result for the same value, and
unsupported semantics block rather than fall back silently.

**Checkpoint:** browser readiness delegates to a storage- and Odoo-independent
evaluator while preserving the prepared bundle and preflight path. Determinism,
adapter parity, lineage, row reconciliation, and blocking issue behavior are
executable. Server-rendered previews and runtime reuse the same scalar
evaluation function. The current materializing adapter fails before loading
when a project exceeds the recorded 100,000-physical-row browser limit. Slice 1
is closed for that bounded browser scope; streaming beyond it remains a later
scale extension.

### Slice 2 — Persist canonical staging and reconciliation

Add durable project-scoped staging in DuckDB with atomic run publication.
Retain raw source pointers, governed values, typed proposed values, lineage,
issues, and deterministic ordering. Process large inputs in bounded batches
and record row-count equations and business control totals.

**Gate:** unchanged inputs produce identical portable evidence; every
transformation explains any created, combined, excluded, or quarantined row.

**Checkpoint:** canonical runs and typed rows are published atomically in
the project DuckDB, retrieved with hash validation, and bound to the readiness
report. Identical current evidence is idempotent; changed evidence supersedes
the current run; bound-input changes invalidate the current pointer while
retaining history. Direct, lookup, parent, and child datasets retain complete
contributing source-row pointers and dataset-level row controls. The Review UI
shows a plain saved confirmation and keeps identifiers, hashes, versions, and
technical controls collapsed. Persistence writes are bounded and batched.
Data managers may optionally declare up to three named expected sums per
dataset by choosing a mapped numeric field and entering its expected value,
unit, and optional tolerance. Results are deterministic, durable, visible in
plain language, and package-blocking when they do not reconcile. Impodo never
guesses business fields or context. Slice 2 is closed for the bounded browser
scope; the evaluator still materializes validated tables in memory.

### Slice 3 — Add quality rules and quarantine

Execute the first applicable families from the
[coverage ledger](data-quality-coverage.md): post-transformation identity
collisions, required values, bounded formats, lookups, cross-field rules, and
relationship readiness. Add immutable quarantine reasons, ownership, expiry,
correction evidence, and rerun behavior.

**Gate:** every physical source row has one accounting entry, every canonical
row reaches exactly one reconciled disposition, and there are no silent drops,
guessed lookups, or unresolved required relationships.

The implementation sequence, dual physical/canonical accounting model,
data-manager UI, persistence design, and acceptance cases are defined in the
[Slice 3 quality and quarantine plan](slice-3-quality-and-quarantine-plan.md).

### Slice 4 — Integrate normalization review

Build the existing dry-run summary from staged evidence, persist immutable
group decisions and whole-run approval, and freeze the exact canonical dataset
hash. Any changed input creates a new run and invalidates approval eligibility.

**Gate:** required correction groups and collisions cannot be bypassed, and a
normalization approval grants no Odoo capability.

### Slice 5 — Run read-only preflight from durable staging

Replace the browser readiness service's transient prepared bundle with frozen
canonical rows adapted to the existing prepared-record and preflight contracts.
Preserve the implemented browser and CLI entry paths, plan metadata once per
model, merge record requirements, split large domains into bounded requests,
and build indexed relationship lookups. Retain target fingerprints and
snapshot hashes with the staged run.

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
