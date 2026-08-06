# Slice 6 plan: scoped advanced coverage and reviewed resolution

## Status and outcome

**Status:** Platform slice completed on 2026-08-06. Project-specific reference
data, validators, anomaly rules, and fuzzy policies still require the explicit
scope approval defined below; the implementation does not invent them.

Slice 6 closes the advanced data-quality gaps required by an approved
migration scope without entering clean-package certification or Odoo
execution. It adds:

- deterministic structural joins, unions, and grouping;
- versioned reference data and captured Odoo selection-key checks;
- versioned domain validators and governed anomaly thresholds;
- bounded fuzzy candidate generation with no automatic fuzzy merge;
- immutable reviewer decisions, field-level survivorship, and governed scalar
  corrections;
- a durable effective-dataset boundary between canonical staging and quality;
- complete lineage, reconciliation, invalidation, restart, and scale evidence.

The resulting source-side flow is:

```text
frozen source datasets
-> submitted mapping + versioned preparation rules + reference data
-> deterministic structural evaluation
-> immutable canonical staging
-> bounded resolution evaluation and reviewer decisions
-> immutable effective dataset with field-level provenance
-> quality and anomaly evaluation
-> normalization review and exact freeze
-> existing read-only Odoo preflight
```

Slice 6 does not certify a clean package, approve an export or import plan,
contact a new target during preparation, write to Odoo, or rehearse an import.
Those boundaries remain in Slice 7 or later execution work.

## Scope approval required before project use

The repository contains the capability ledger but no approved
organization-specific migration scope. Slice 6 must not guess which business
reference lists, validators, anomaly thresholds, join shapes, or fuzzy fields
are authoritative.

The first deliverable is therefore a versioned `CoverageScopeRevision` for
each project or reusable migration archetype. It records:

- every applicable coverage-ledger family and every explicitly inapplicable
  family, with rationale;
- the source datasets and target business objects in scope;
- required structural operations and their expected row-count equations;
- authoritative reference-data owners and versions;
- exact domain-validator identifiers, jurisdictions, and proof boundaries;
- anomaly metrics, thresholds, comparison populations, owners, and outcomes;
- fuzzy blocking fields, comparison fields, weights, and review owners;
- survivor fields, permitted sources, tie policies, and fields on which a
  governed reviewer correction is allowed;
- data classification, masking, retention, and approval roles;
- acceptance fixtures and declared business control totals.

Changing this scope revision invalidates all downstream preparation evidence.
An inapplicable declaration is a reviewed decision, not a default caused by a
missing rule.

The recommended first platform tranche targets these ledger gaps:

| Ledger family | Slice 6 target after all gates pass |
| --- | --- |
| `TC-09` dictionaries, lookups, and selection values | Versioned exact references and frozen-schema technical-key validation |
| `TC-12` multi-table and shape transformations | Bounded joins, union-all, grouping, cardinality, lineage, and reconciliation |
| `TC-14` fuzzy entity resolution | Bounded deterministic candidates, explicit accept/reject evidence, and false-match fixtures |
| `TC-15` survivorship and consolidation | Reviewed merges and complete field-level survivor provenance |
| `TC-16` domain-specific validation | Only the validators named in the approved scope, with explicit proof limits |
| `TC-19` cross-field and cross-row rules | Structural and aggregate rules integrated with existing guided checks |
| `TC-20` distribution and anomaly controls | Versioned deterministic thresholds and review evidence |
| `TC-22` correction and reprocessing | Governed scalar correction overlays and immutable reruns; raw and staged evidence remains unchanged |
| `TC-23` repeatability and bounded scale | A measured advanced-coverage run at the supported browser limit |

The coverage ledger remains unchanged until integrated behavior, tests,
business examples, retained evidence, performance, and user guidance satisfy
its strict `VERIFIED` definition.

## Architectural decisions locked for this slice

### Reuse the current semantic authorities

Do not add user-authored Python, SQL, regular-expression code, formulas, or a
second general transformation language.

- Evolve `DerivedEntityPlan` into the existing authority for bounded source
  preparation rules. Add structural rule variants while preserving its
  immutable revisions, source-selection binding, preview/runtime parity, and
  existing lookup and parent/child rules.
- Extend mapping fields and relationship resolvers with references to exact
  versioned reference-data hashes. Do not copy a mutable external list at run
  time.
- Extend `QualityRuleSet` with allowlisted domain and anomaly families. Keep
  quality as a non-mutating classification layer.
- Add one resolution-policy contract for candidate generation, review, and
  survivorship. It may consume canonical staging but may never rewrite it.
- Compile browser and expert-profile inputs into the same portable contracts.
  Unsupported expert-profile syntax must fail instead of creating different
  behavior.

All new contracts are canonical-JSON serializable, versioned, content-hashed,
deterministically ordered, free of credentials and numeric Odoo IDs, and
validated before any row processing begins.

### Structural operations execute before canonical publication

The initial structural allowlist is deliberately narrow:

- exact one-to-one or many-to-one `LEFT` and `INNER` equijoins on typed keys;
- `UNION ALL` through explicit compatible column projections;
- grouping on explicit typed keys with `COUNT` and decimal `SUM` aggregates;
- an explicit unmatched-row policy of block, quarantine, or governed
  exclusion;
- explicit expected input, matched, unmatched, output, and aggregate control
  equations.

Many-to-many joins, cross joins, range joins, temporal joins, recursive joins,
implicit deduplication, arbitrary aggregators, and source-order-dependent
"first row" behavior are out of scope.

For each join:

- the declared right-side key must be unique for one-to-one or many-to-one
  cardinality before output rows are emitted;
- duplicate right keys block the affected operation rather than multiply
  left-side rows;
- unmatched rows follow the declared policy and remain accounted for;
- every output row retains all contributing physical source pointers;
- a right-side row reused by several left rows remains one accounted source
  row with explicit fan-out links;
- output order and identifiers derive from typed keys and source hashes, never
  incidental file or database order.

For grouping, every input row contributes to exactly one group or one explicit
set-aside state. Decimal sums retain declared precision, unit, and currency
context; missing or mixed context blocks. Counts and declared sums must
reconcile before staging publication.

### Reference data is immutable input evidence

A `ReferenceDataSet` is a project-owned or approved reusable package with a
stable identifier, version, content hash, key columns, value columns, owner,
classification, effective label, and bounded rows. It is created from frozen
registered source evidence or an audited inline list; it is never fetched
silently from the internet or Odoo during preparation.

Every lookup declares:

- exact typed input keys and output values;
- key uniqueness and normalization rules;
- unknown, blank, and ambiguous outcomes;
- the exact reference-data content hash;
- whether the result is a display value, portable business key, or Odoo
  selection technical key.

Duplicate reference keys block publication. Unknown required values never
fall back to a display label or guessed Odoo key. When a frozen captured Odoo
schema contains selection values, output technical keys are checked against
that snapshot during Slice 6. Slice 7 must recheck them against the exact
authorized rehearsal target.

### Domain validation is deterministic and proof-bounded

Domain validators are named, versioned built-ins with typed parameters. The
approved scope must name each validator and its jurisdiction. Typical initial
families may include checksum, country-scoped identifier, postal pattern,
date-window, and approved-code-list validation, but only accepted scope entries
are implemented as product claims.

A validator:

- performs no network or target call;
- never changes a value;
- states what it proves and what it does not prove;
- identifies its locale, jurisdiction, algorithm version, and accepted input
  type;
- produces a deterministic outcome and masked evidence;
- blocks at configuration time when required context is absent.

Syntax validation must not be described as evidence that an account,
organization, address, or tax registration exists.

### Anomaly rules are governed checks, not automatic corrections

The initial anomaly allowlist covers declared count, distinct-count, null-rate,
duplicate-rate, minimum, maximum, sum, and interquartile-range boundaries over
an explicit dataset or group. Thresholds and comparison populations are
versioned inputs, not learned silently from prior customer data.

An anomaly may warn, block, or quarantine according to its approved rule. It
never edits a value, creates a merge, or excludes a row automatically. Every
result retains the metric, population count, threshold, rule version, owner,
and masked examples. Boundary comparisons use typed decimal arithmetic and
explicit inclusive/exclusive semantics.

### Fuzzy matching only creates bounded candidates

Fuzzy evaluation runs only for entity types and fields declared in the
resolution policy. It uses:

- Unicode NFKC normalization, case-folding, whitespace collapse, and only the
  field-specific punctuation policy stored in the contract;
- one or more exact blocking keys, such as country plus postal prefix;
- versioned normalized-Levenshtein and token-Jaccard scorers;
- declared decimal weights, candidate threshold, and deterministic tie order;
- at most 50 records in any comparison block and at most 5 retained candidates
  per record in the initial browser scope.

An oversized block stops with a setup finding and requires a more selective
blocking rule. The evaluator never falls back to all-pairs comparison. Pair
scores are quantized and ordered by score followed by stable row hashes, so an
unchanged input produces identical candidates.

Fuzzy output is evidence only. A high score, a unique top score, or an exact
normalized display-name match never merges records automatically. Numeric Odoo
IDs, target search results, and live Odoo values are not candidate inputs.

### Resolution creates an immutable effective dataset

Canonical staging remains immutable evidence of mapping and structural
evaluation. Add a reviewed resolution layer after staging and before quality:

1. `ResolutionEvaluation` records candidate pairs, possible groups, conflicts,
   required field choices, and the exact policy and staging hashes.
2. append-only `ResolutionDecision` records accept, reject, keep-separate,
   field-source, and allowed correction decisions with actor, reason, time,
   and optimistic lifecycle version;
3. freezing a complete review publishes an immutable effective dataset and
   content hash;
4. quality, normalization, and preflight bind to that effective dataset rather
   than silently substituting raw staging rows.

Projects with no candidate or correction rule still publish a deterministic
pass-through resolution result. Pass-through rows retain their canonical row
identity. Survivors and corrected rows receive deterministic effective row IDs
derived from the policy, contributing rows, field decisions, and values.

Every staged row has exactly one resolution state: passed through, kept
distinct, or contributed to one survivor. A row cannot contribute to two
survivors. Rejected candidate pairs remain distinct rows.

Every effective field records one provenance kind:

- `COPIED` from one canonical row;
- `UNANIMOUS` when all accepted contributing rows carry the same typed value;
- `SELECTED_SOURCE` through an explicit field decision;
- `REVIEWER_CORRECTION` for an allowed typed scalar replacement;
- `STRUCTURAL_AGGREGATE` with all contributing lineage and aggregation rule.

Provenance contains source row and field hashes, the resolution group and
decision IDs where applicable, actor evidence for reviewed values, and the
result value hash. A correction requires a reason, runs through the same type
and validation boundary as mapped values, and never alters registered source
bytes or canonical staging. Identity and relationship values are selection
only in this slice; changing their meaning requires a new mapping or approved
resolution policy and rerun.

Required fields, identities, scopes, relationships, and control totals are
reconciled again over the effective rows. An incomplete survivor cannot be
frozen.

## Durable contracts and hash bindings

Introduce or evolve these portable contracts:

- `CoverageScopeRevision` — applicability and approval of capability
  families;
- structural variants within `DerivedEntityPlan` — exact join, union-all, and
  group/aggregate rules;
- `ReferenceDataSet` and `ReferenceBundle` — exact lookup content and owner
  evidence;
- domain and anomaly variants in `QualityRuleSet`;
- `ResolutionPolicy` — blocking, scoring, review, survivor, and correction
  rules;
- `ResolutionEvaluation` — immutable candidate and conflict evidence;
- `ResolutionDecision` — append-only reviewed choices;
- `EffectiveDatasetSummary`, `EffectiveRow`, and `FieldProvenance` — the exact
  post-resolution rows consumed downstream.

The binding chain becomes:

```text
source selection
  + coverage scope
  + preparation plan
  + mapping and schema
  + reference bundle
      -> canonical staging
          + resolution policy and decisions
              -> effective dataset
                  + quality rules and retention context
                      -> quality run
                          -> normalization freeze
                              -> read-only preflight
```

Every downstream content hash includes all consequential upstream semantic
hashes. Actor names and timestamps remain lifecycle evidence and are excluded
from semantic equality where the existing contracts make that distinction.
Portable evidence recursively rejects credentials and numeric Odoo IDs.

## Persistence, migration, and invalidation

Use the advanced-coverage project migration for the complete Slice 6 persistence
shape. It adds immutable revision/run headers, distinct current pointers,
bounded child relations, and append-only audit events for scope, references,
resolution, decisions, effective rows, and field provenance.

Store pass-through effective rows by canonical-row reference and store a full
effective payload only for survivor or corrected rows. Repository reads expose
one uniform effective-row iterator in bounded batches. This avoids duplicating
every 25,000-row canonical payload while preserving restart and source-deletion
behavior.

Publication is atomic and optimistic:

- candidate, group, decision, effective-row, and provenance records are
  inserted and verified in bounded batches;
- a resolution decision supplies the current lifecycle version;
- only a complete, conflict-free review may advance the current effective
  dataset pointer;
- failed evaluation, decision, freeze, or persistence preserves the previous
  successful current evidence;
- changing a frozen decision requires a new evaluation and retained history;
- normal page reads use counts and bounded result pages, not full-run JSON.

Invalidation follows this matrix:

| Changed evidence | Resolution/effective dataset | Quality | Normalization | Preflight |
| --- | --- | --- | --- | --- |
| Source selection, preparation plan, mapping, schema, or reference bundle | Invalidate current; retain history | Invalidate | Invalidate | Invalidate |
| Coverage scope or resolution policy | Invalidate current; retain history | Invalidate | Invalidate | Invalidate |
| Resolution decision before freeze | Advance lifecycle only | Not current yet | Not current yet | Not current yet |
| Effective dataset freeze | Publish exact current hash | Require new run | Require new run | Invalidate old current |
| Domain, anomaly, ownership, classification, masking, or retention rule | Preserve effective data when semantics allow | Invalidate | Invalidate | Invalidate |
| Normalization decision | Preserve | Preserve | Existing Slice 4 lifecycle applies | Existing Slice 5 invalidation applies |
| UI filter, page, search, or artifact projection | Preserve | Preserve | Preserve | Preserve |

Older schema-v19 projects remain readable. Historical quality, normalization,
and preflight runs stay historical; they are not falsely rebound to a new
effective dataset. A new preparation publishes the deterministic pass-through
resolution evidence when no advanced policy applies.

## Data-manager journey and authorization

Keep the existing workflow labels. Add advanced authoring under collapsed,
plain-language sections rather than changing the global navigation:

- **Combine tables** for approved join, union, and grouping rules;
- **Reference lists** for exact versioned translations;
- **Additional data checks** for approved domain and anomaly rules;
- **Possible duplicate rules** for bounded candidate policies.

After **Prepare and review data**:

- if no resolution decision is needed, continue automatically to quality and
  normalization;
- if candidates or permitted corrections need review, show one next action,
  **Review possible duplicates**;
- show each candidate with masked side-by-side business fields, score
  explanation, and **Same record** / **Keep separate** decisions;
- after an accepted group, show only conflicting survivor fields and require a
  source choice or allowed typed correction;
- after the review is complete, publish the effective dataset and continue to
  the existing prepared-value review;
- keep rule IDs, hashes, algorithms, technical field names, and raw score
  components under **Support details**.

Add explicit capabilities:

- `coverage.scope` for applicability and advanced-rule approval;
- `resolution.decide` for pair, group, field, and correction decisions;
- `resolution.approve` for freezing the effective dataset.

Existing `normalization.decide`, `normalization.approve`, and `preflight.run`
remain separate. Resolution approval grants no Odoo access and no package,
export, or execution approval.

## Implementation sequence

### 6A - Approve scope and lock contracts

Add the scope, reference, resolution, effective-row, and field-provenance
contracts; extend serialization and portable-value validation; define exact
hash and reconciliation equations; create representative accepted-scope
fixtures.

**Gate:** every consequential input has one semantic authority and hash;
fixtures name concrete validators, references, structural shapes, fuzzy
fields, and owners; unsupported scope entries fail before row processing.

### 6B - Add structural preparation and versioned references

Extend `DerivedEntityPlan`, mapping compilation, browser authoring, previews,
and full-row staging with exact joins, union-all, grouping, reference bundles,
lineage, and row/control reconciliation.

**Gate:** duplicate join keys cannot multiply output, unmatched rows remain
accounted for, group contributors are complete, reference ambiguity blocks,
and preview/runtime/profile fixtures are equivalent.

### 6C - Build bounded candidate generation

Implement the resolution policy compiler, deterministic blocking and scoring,
candidate/group evidence, oversize-block failures, repository batching, and
restart retrieval. Do not implement a merge shortcut.

**Gate:** candidate generation is deterministic and bounded; at most five
candidates are retained per row; blocks above 50 records stop; every fuzzy
candidate remains separate without an explicit reviewed decision.

### 6D - Add decisions, survivorship, and effective rows

Implement optimistic decisions, conflict detection, survivor field choices,
allowed scalar corrections, field provenance, effective-row publication,
post-resolution reconciliation, capabilities, and invalidation.

**Gate:** every effective value explains its source and decision; no row joins
two survivors; rejected candidates stay distinct; incomplete or stale reviews
cannot freeze; raw and canonical evidence remains unchanged.

### 6E - Extend quality and bind all downstream stages

Add only the approved domain-validator and anomaly families to
`QualityRuleSet`. Change quality to evaluate exact effective rows, then evolve
normalization and `FrozenPreflightInput` to bind and adapt that same effective
dataset. Preserve existing behavior through the pass-through case.

**Gate:** quality never mutates effective rows; corrections and survivors are
fully revalidated; normalization freezes the exact effective dataset; preflight
performs zero source evaluation and uses only frozen effective rows.

### 6F - Complete the data-manager journey

Add advanced authoring, possible-duplicate review, survivor-field review,
bounded pages, plain summaries, one-next-action routing, authorization, masked
evidence, and support details. Reuse the settled Review and normalization
components.

**Gate:** a data manager can configure an approved case, reject a false match,
select survivor values, explain an allowed correction, and continue to the
existing review without knowing algorithms, hashes, DuckDB, or Odoo IDs.

### 6G - Acceptance, scale, and documentation

Run the focused contract, repository, application, web, parity, invalidation,
concurrency, restart, security, and performance suites. Add accepted business
fixtures for each declared scope family and update contracts, operations,
coverage baselines, and code maps only after the gates pass.

**Gate:** the full suite is green; the complete advanced source-side flow is
measured at the supported browser limit; all portable evidence remains safe;
coverage statuses change only where the strict ledger definition is met.

## Required acceptance cases

### Scope and contracts

- preparation refuses a missing or stale applicable-family declaration;
- inapplicable declarations retain reviewer, rationale, and version;
- changing scope, rules, references, algorithm version, weights, thresholds,
  or permitted corrections invalidates downstream current pointers;
- browser and expert-profile inputs compile to equal portable semantics for
  equivalent fixtures;
- unsupported executable expressions, SQL, code, and arbitrary algorithms are
  rejected.

### Structural and reference behavior

- one-to-one and many-to-one joins emit no more rows than the declared left
  population;
- duplicate right keys block before output publication;
- matched, unmatched, quarantined, excluded, and output counts reconcile;
- every joined and grouped output retains all physical source pointers;
- union-all requires compatible explicit projections and reconciles branch
  counts;
- grouping is independent of source order and uses typed decimal arithmetic;
- mixed currency or unit context blocks an aggregate;
- duplicate, blank, ambiguous, unknown, stale, or tampered reference evidence
  follows the declared safe outcome;
- captured selection labels cannot substitute for technical keys;
- a reference version change produces new staging semantics and retained
  history.

### Domain and anomaly behavior

- each validator proves only its documented syntax or checksum boundary;
- jurisdiction, locale, or type absence blocks configuration;
- validation results are deterministic and masked according to classification;
- anomaly threshold equality follows explicit inclusive/exclusive semantics;
- group populations and metrics reconcile with effective rows;
- warnings do not exclude, quarantine, correct, or merge records implicitly;
- rule changes create new immutable evidence and invalidate approvals.

### Candidate and review behavior

- unchanged inputs and policies yield identical pairs, scores, ordering, and
  group IDs;
- fuzzy work is bounded by declared blocking keys, 50-record blocks, and five
  retained candidates per row;
- oversized or blank blocking keys fail safely without all-pairs comparison;
- threshold, tie, punctuation, Unicode, token, multilingual, and false-positive
  fixtures are covered;
- candidate scores never cause an automatic merge;
- accept, reject, keep-separate, conflicting overlap, stale lifecycle, missing
  capability, and concurrent-decision cases are covered;
- rejected candidates remain independent effective rows;
- decision history survives restart and normal review uses bounded pages.

### Survivorship, correction, and downstream binding

- unanimous, selected-source, corrected, null, relationship, and aggregate
  fields retain complete provenance;
- reviewer corrections require an allowed field, typed valid value, actor,
  reason, and immutable before/after evidence;
- identity and relationship meaning cannot be free-typed in the review;
- a staged row cannot be lost or contribute to more than one survivor;
- every effective row and field hash recomputes after restart;
- incomplete required fields, identity collisions, relationship conflicts, or
  failed control totals prevent effective-dataset freeze;
- quality evaluates the effective dataset and never rewrites it;
- normalization and preflight bind the exact resolution and effective hashes;
- preflight succeeds from durable evidence after source deletion and performs
  no resolution, correction, or source transformation;
- any effective-row tamper or stale binding stops before Odoo access;
- pass-through projects preserve existing results and classifications;
- portable manifests, workbooks, logs, and normal pages contain no credential
  or numeric Odoo ID leakage.

## Scale and operational gates

Keep the verified browser limit at 25,000 physical source rows. The separate
100,000-row plan continues to own any increase.

The deterministic workstation probe must include at least one bounded join,
one grouping, one versioned reference set, approved domain and anomaly rules,
fuzzy blocks near their maximum, reviewed candidates, survivor fields, a
correction, quality, normalization, restart retrieval, and durable effective
rows.

Retain these structural guards:

- no repository, reference lookup, scoring setup, or validator construction
  inside a per-row external-access loop;
- no all-pairs fuzzy comparison and no full-run JSON load for a normal page;
- stable counts for input rows, comparisons, candidates, groups, decisions,
  survivor rows, provenance rows, quality issues, and stored bytes.

Time, working set, and database size remain recorded operational diagnostics,
not Slice 6 pass/fail gates. Prefer bounded iterators, indexed DuckDB queries,
compact field hashes, and pass-through row references, and retain measurements
so later performance work has comparable evidence.

## Explicitly out of scope

- clean-package certification and coverage release claims before all evidence
  gates pass;
- authorized target rehearsal, freshness policy, and import-plan approval;
- any `create`, `write`, `unlink`, import, RPC mutation, or production Odoo
  access;
- target numeric IDs in portable source-side contracts;
- arbitrary SQL, Python, formulas, scripts, regular-expression code, or
  customer-supplied validator execution;
- many-to-many, temporal, range, recursive, or unbounded joins;
- automatic fuzzy merging, automatic anomaly correction, or inferred survivor
  values;
- machine-learning matching, cross-customer learning, or network validation;
- broad 100,000-row support without the separate measured refactor gates;
- global navigation or unrelated visual redesign.

## Definition of done

Slice 6 is complete only when:

- an approved scope revision exists and every selected family has accepted
  business fixtures;
- structural and reference rules publish deterministic reconciled staging;
- fuzzy candidates are bounded and never merged automatically;
- reviewer decisions and effective rows are durable, authorized, restartable,
  and completely provenance-bound;
- domain and anomaly checks evaluate the exact effective dataset;
- normalization and preflight consume the exact frozen effective rows;
- invalidation and optimistic concurrency preserve history and prevent stale
  publication;
- the complete supported-scale probe and full local suite pass;
- contracts, code maps, user guidance, and the coverage ledger reflect only
  demonstrated behavior;
- no part of Slice 6 certifies, imports, writes to, or otherwise changes Odoo.

## Completion evidence

The completed implementation provides the scope and reference contracts,
structural evaluation, exact reference lookup compilation, bounded fuzzy
candidates, append-only reviewed decisions, survivor and correction
provenance, compact durable effective rows, advanced quality families, and
effective-dataset binding through normalization and read-only preflight. The
browser redirects incomplete candidate reviews to a masked, authorized review
journey and resumes preparation only after an exact freeze.

The discovered local suite passed after the Slice 6 changes. A deterministic
25,000-row advanced preparation run also completed with all 25,000 rows staged,
quality-ready, normalization-eligible, and durably reloadable. It took 92.811
seconds, observed a 1,133.8 MiB peak working set, and produced a 108.5 MiB
project database. These are retained diagnostics rather than release gates.

Passing this definition makes Slice 7 implementation eligible. It does not by
itself make any migration package certified or executable.
