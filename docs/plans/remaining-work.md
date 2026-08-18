# Impodo remaining work

## Status and authority

**Status:** Active roadmap from 2026-08-10.

This is the authoritative cross-product roadmap in `docs/plans/`. Scoped
implementation proposals may live beside it, but they do not change this
roadmap's priority order unless explicitly adopted here. Completed
implementation belongs in Git, release evidence belongs in `docs/reports/`
and `docs/testing/`, and current behavior belongs in architecture, contracts,
and audience-appropriate user or developer documentation.

## Current boundary

Impodo supports the reviewed disposable local and remote Odoo 19 load and
read-back path. Retained remote on-premises acceptance evidence is still
pending until the intended target is available.

The current preparation limits are:

- 100,000 physical rows for exact-snapshot direct mappings compiled entirely
  to the native columnar path;
- 50,000 physical rows for direct mappings requiring the Python oracle;
- 25,000 physical rows for derived or materialized paths.

The next unconditional goal is to qualify complete related and mixed-dataset
workflows at 100,000 physical rows without changing evidence semantics.

The scoped [Odoo source import and round-trip update implementation
plan](odoo-source-import-plan.md) tracks existing Odoo records as governed
Impodo source data. Capture and offline pinned preparation are
implemented through Phase 5; protected three-way comparison and later write
phases remain planned. This work does not displace the unconditional
preparation-scale goal above.

The scoped [high-volume transformation architecture implementation
plan](transformation-scale-architecture-plan.md) expands the first goal with a
weighted comparison of four architectures, a measured hash policy, a
columnar/set-based target design, and a phased route for qualifying 16,000
Products plus 80,000 related BOM lines. It does not raise the current limits
until its acceptance gates pass.

<a id="1-build-the-columnar-preparation-path-and-raise-the-scale-limit"></a>

## 1. Qualify related and mixed preparation at 100,000 rows

### Outcome

Prepare, validate, normalize, and durably publish Products, related BOM, and
mixed-dataset workloads in less than 120 seconds and below 900 MiB peak working
set on the reference Windows workstation. Identical inputs must produce
identical portable evidence, and failed publication must leave the last valid
evidence current.

### 1.1 Complete comparable performance evidence

- Define the reference Windows workstation and freeze the benchmark fixture
  revisions.
- Add the missing mixed fixture covering relationships, derived entities,
  grouping, multi-source lineage, ambiguity, and transformation effects.
- Run three clean fresh-process measurements for Products, a genuinely related
  BOM workflow, and the mixed fixture at the applicable 50,000/100,000-row
  boundaries. Retain the 4,000-row effect-heavy workbook as the regression
  case.
- Record revision, fixture checksum, Python environment, batch sizes, CPU and
  wall time by phase, peak and ending working set, database size, counts, and
  hashes for every run.
- Measure original-file ingestion, first preparation, repeated preparation
  from the same snapshots, and effect-heavy preparation separately.
- Keep second-platform results as regression evidence, not as a substitute for
  the Windows acceptance runs.

### 1.2 Bound related and derived preparation

- Route relationship resolution, parent grouping, derived hierarchies, aliases,
  relationship edges, and multi-source lineage through the durable preparation
  session instead of the materializing browser path.
- Build one bounded `(dataset, source identity)` index and cache recursive
  target-key calculations. Do not scan parent records once per child or issue
  per-row target lookups.
- Preserve exact row IDs, ordering, lineage, reconciliation, issues, effects,
  control totals, and hashes across batch sizes `1`, `17`, and the production
  default.
- Prove missing, ambiguous, cyclic, fan-out, and deep relationship cases,
  including fail-closed handling where a required parent cannot be resolved.
- Remove the duplicate materializing production path after parity, failure,
  and scale gates pass; retain a compact test oracle where useful.

### 1.3 Finish bounded quality and normalization for mixed workflows

- Consume related and derived canonical rows in bounded batches without
  rebuilding the complete run in memory.
- Retain only compact global indexes required for cross-row identity and
  relationship rules.
- Emit quality results, source accounting, transformation effects, and
  normalization effects through bounded sinks.
- Construct each normalization effect once while accumulating group counts and
  bounded examples; do not retain complete impact and candidate collections.
- Preserve atomic publication, restart safety, typed values, deterministic
  ordering, and exact content hashes.

### 1.4 Profile before further persistence changes

Profile the complete mixed workflow after the Windows runs. Implement only
measured bottlenecks:

- remove repeated row decoding or effect construction;
- reuse bounded cursors where measurement shows a benefit;
- reduce connection, commit, or checkpoint overhead without holding a
  transaction across Odoo access;
- keep row-count and byte-count bounds for every transport envelope.

### 1.5 Complete useful progress reporting

- Extend monotonic, completed-work progress beyond source transformation to
  related publication, quality, and normalization batches.
- Keep the main journey in business language and do not expose storage or
  backend choices to the operator.
- Add a deliberately slowed browser test proving that progress continues and
  never advances ahead of durable work.

### 1.6 Close the related and mixed 100,000-row gate

The broader limit may change only after all of these pass:

- three fresh-process Windows runs of Products, related BOM, and the mixed
  fixture each finish below 120 seconds and 900 MiB peak working set;
- batch size does not change related or derived portable evidence;
- missing/ambiguous relationships, cancellation, injected failure, retry,
  stale-session cleanup, and concurrent-pointer tests preserve the last valid
  run;
- the focused, full, browser, security, fault-injection, deterministic, and
  opt-in scale suites pass;
- deterministic local Odoo comparison performs no source preparation and
  finishes within its separate 120-second local-processing gate;
- developer runbooks, acceptance evidence, limits, and user-facing messages are
  updated in the same change.

Until then, retain the current 100,000-row limit only for verified native
columnar direct mappings, 50,000 for Python-fallback direct mappings, and
25,000 for derived or materialized paths.

## 2. Add optional clean-package certification

### Trigger

Implement this track only for a migration or organization that requires a
formal **clean for Odoo target rehearsal** claim. It must not block the routine
disposable-target load path.

### Remaining work

- Approve a versioned coverage scope for the concrete migration, including
  applicable and inapplicable data-quality families and their owners.
- Supply authoritative reference data, locale/currency/unit policy, domain
  validators, anomaly thresholds, fuzzy-match fields, and accepted business
  fixtures for that scope.
- Complete project-specific proof for:
  - localization, types, precision, dates, timezones, and defaults;
  - joins, calculations, hierarchy, cross-field, and cross-row rules;
  - reference translations, fuzzy decisions, survivorship, and anomalies;
  - Odoo company, currency, unit, selection, constraint, and default behavior;
  - privacy, retention, correction, reprocessing, scale, and reconciliation.
- Create a hash-bound package certificate and approval lifecycle for the exact
  frozen source, mapping, rules, decisions, target schema, and target snapshot.
- Invalidate the certificate whenever any bound input changes.
- Run an authorized rehearsal against the exact Odoo target and retain its
  reconciliation evidence.

### Gate

A package may be certified only when every physical and canonical row is
accounted for, no blocking or ambiguous condition remains, every reviewed
exception has evidence, control totals reconcile, all applicable coverage is
verified, privacy controls pass, and the exact package passes target rehearsal.

## 3. Complete remote acceptance and production readiness

### 3.1 Remote acceptance and operational hardening

- Run the sanitized representative harness against a fresh disposable remote
  target and retain its target fingerprint, reconciliation, repeat-preview,
  and throughput evidence.
- Confirm the target backup or restore point before each retained acceptance
  run.
- Define pause, resume, uncertain-outcome recovery, and operator guidance for
  remote failures.
- Inject failures at send, commit, journal, read-back, credential expiry,
  overload, and schema-change boundaries.
- Prove Odoo ACLs, record rules, field access, company scope, credential
  redaction, cross-workstation target locking, and rejection of
  caller-controlled methods or context.
- Measure remote lookup and write counts. Pre-resolve governed business keys in
  bounded batches and prevent N+1 target calls before production-scale runs.
- Add adaptive bounded batches only where measurements justify them; isolate a
  bad row without silently changing transaction or retry semantics.

**Gate:** a remote run never reports success from an HTTP response alone,
never retries an unknown write blindly, and reconciles every proposed row.

### 3.2 Production readiness

- Complete threat modeling, privacy assessment, penetration testing, customer
  security review, observability, retention, disaster recovery, release and
  rollback procedures, and representative customer acceptance.
- Define the conditions that promote a run from routine to standard or
  controlled assurance.
- Add separately reviewed support for business actions beyond create and
  explicit update, such as posting or workflow transitions. General model and
  field support must remain preview-derived; do not reintroduce a global model
  or field allowlist.

**Gate:** each added business action has explicit permissions, transaction
semantics, failure handling, reconciliation, rehearsal, and operational proof.

## 4. Conditional architecture work

These items remain parked until a concrete deployment requires them.

### Target-side gateway

Build the signed Odoo 19 add-on, manifest-bound grants, target-side receipts,
and named business-action handlers only when native JSON-2 cannot provide the
required atomicity or idempotency. The gateway must expose no generic RPC,
SQL, `sudo`, or caller-selected method surface.

### Hosted composition

Add PostgreSQL repositories, object storage, durable workers, distributed
target locks, SSO actors, centralized authorization, and managed secrets only
for a hosted deployment. DuckDB and PostgreSQL compositions must produce
semantically identical portable evidence and pass the same contract, fault,
security, and Odoo integration suites.

## Decisions needed when their track starts

1. Which Windows workstation and fixture revisions are the scale-release
   reference?
2. Which real migration first requires clean-package certification, and who
   owns each scope-specific rule and reference dataset?
3. Which remote target, backup evidence, assurance triggers, and recovery
   expectations define the first production profile?
4. Which concrete business action, if any, justifies a target-side gateway?
5. Which deployment requirement, if any, justifies the hosted composition?
