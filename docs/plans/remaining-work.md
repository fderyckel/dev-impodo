# Impodo remaining work

## Status and authority

**Status:** Active roadmap from 2026-08-06.

This is the only planning document in `docs/plans/`. It contains work that is
not yet complete. Completed implementation history belongs in Git, release
evidence belongs in `docs/reports/` and `docs/testing/`, and current behavior
belongs in architecture, contracts, and operations documentation.

The order below is intentional. Finish the measured local scale boundary
before expanding target risk. Clean-package certification, a target-side
gateway, and hosted deployment are conditional capabilities, not prerequisites
for an ordinary disposable-local migration.

## Current boundary

Impodo currently supports the complete reviewed local Odoo 19 load and
read-back path. Its native writer accepts any standard, extension, or custom
model and field present in the captured schema and exact reviewed preview.

The supported preparation limit is:

- 50,000 physical rows for the bounded direct-dataset path;
- 25,000 physical rows for derived or materialized paths.

The next unconditional product goal is to make the complete Products, BOM,
and mixed related-dataset workflows pass the 100,000-row release gate without
changing evidence semantics.

## 1. Finish bounded preparation and raise the scale limit

### Outcome

Prepare, validate, normalize, and durably publish 100,000 physical source rows
in less than 120 seconds and below 900 MiB peak working set on the reference
Windows workstation. Identical inputs must still produce identical portable
evidence, and failed publication must leave the last valid evidence current.

### 1.1 Complete comparable performance evidence

- Run three fresh-process measurements on the reference Windows workstation
  for the 4,000-row effect-heavy workbook and the 50,000/100,000-row fixtures.
- Record revision, fixture checksum, Python environment, batch sizes, elapsed
  time by phase, peak and ending working set, database size, counts, and hashes.
- Add any missing bounded subphase counters needed to separate evaluation,
  transport, hashing, commit, and checkpoint time without exposing row values.
- Add the missing mixed related-dataset fixture. It must exercise relationships,
  derived entities, grouping, multi-source lineage, and ambiguity behavior.
- Keep second-platform results as regression evidence, not as a substitute for
  the Windows acceptance runs.

### 1.2 Bound related and derived preparation

- Route BOM relationships, parent grouping, derived hierarchies, aliases,
  relationship edges, and multi-source lineage through the durable preparation
  session.
- Preserve exact row IDs, ordering, lineage, reconciliation, issues, effects,
  control totals, and hashes across batch sizes `1`, `17`, and the production
  default.
- Prove missing, ambiguous, cyclic, fan-out, and deep relationship cases.
- After parity and failure gates pass, remove the duplicate materializing
  browser path and retain only a test oracle where it is still useful.

### 1.3 Bound quality and normalization

- Evaluate verified durable canonical rows in bounded batches instead of
  reconstructing the complete canonical run in memory.
- Retain only compact global indexes required for cross-row identity and
  relationship rules.
- Emit quality results, source accounting, transformation effects, and
  normalization effects through bounded sinks.
- Construct each normalization effect once while accumulating group counts and
  bounded examples; do not retain complete impact and candidate collections.
- Preserve atomic publication, restart safety, typed values, deterministic
  ordering, and exact content hashes.

### 1.4 Profile before further persistence changes

Do not mechanically convert more DuckDB tables. Profile the complete workflow
after the Windows runs, then implement only demonstrated remaining
bottlenecks:

- remove repeated row decoding or effect construction;
- reuse a bounded cursor where it improves measured behavior;
- reduce connection, commit, or checkpoint overhead without holding a
  transaction across Odoo access;
- keep row-count and byte-count bounds for every transport envelope.

### 1.5 Report useful progress

- Publish monotonic sub-progress from completed batches, such as saving
  prepared rows, running checks, grouping changes, and verifying evidence.
- Keep the main journey in business language; do not expose DuckDB or transport
  terminology to the operator.
- Add a deliberately slowed browser test proving that progress continues and
  never advances ahead of durable work.

### 1.6 Close the 100,000-row gate

The scale limit may change only after all of these pass:

- three fresh-process runs of Products, BOM, and the mixed fixture each finish
  below 120 seconds and 900 MiB peak working set;
- batch size does not change portable evidence;
- source mismatch, cancellation, injected failure, retry, stale-session
  cleanup, and concurrent-pointer tests preserve the last valid run;
- the focused, full, browser, security, and opt-in scale suites pass;
- the deterministic local Odoo comparison performs no source preparation and
  finishes within its separate 120-second local-processing gate;
- operations, acceptance evidence, limits, and user-facing messages are
  updated in the same change.

Until then, retain the 50,000-row direct and 25,000-row derived/materialized
limits.

## 2. Add optional clean-package certification

### Trigger

Implement this track only for a migration or organization that requires a
formal **clean for Odoo target rehearsal** claim. It must not block the routine
local load path.

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

## 3. Support remote and production Odoo loading

### 3.1 Standard remote profile

- Add remote target fingerprinting, secure credential handling, TLS checks,
  target confirmation, and backup or restore-point confirmation.
- Define pause, resume, uncertain-outcome recovery, and operator guidance for
  remote failures.
- Add adaptive bounded batches only where measurements justify them; isolate a
  bad row without silently changing transaction or retry semantics.
- Inject failures at send, commit, journal, read-back, credential expiry,
  overload, and schema-change boundaries.
- Prove Odoo ACLs, record rules, field access, company scope, credential
  redaction, target locking, and rejection of caller-controlled methods or
  context.

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

Build the signed Odoo add-on, manifest-bound grants, target-side receipts, and
named business-action handlers only when native JSON-2 cannot provide the
required atomicity or idempotency. The gateway must expose no generic RPC,
SQL, `sudo`, or caller-selected method surface.

### Hosted composition

Add PostgreSQL repositories, object storage, durable workers, distributed
target locks, SSO actors, centralized authorization, and managed secrets only
for a hosted deployment. DuckDB and PostgreSQL compositions must produce
semantically identical portable evidence and pass the same contract, fault,
security, and Odoo integration suites.

## Cross-cutting delivery rules

- Preserve registered source bytes and never use direct Odoo database writes.
- Keep one semantic authority for mapping, preparation, quality, and
  normalization behavior.
- Bind every result to exact inputs and fail closed on stale evidence.
- Keep portable evidence free of credentials and numeric Odoo record IDs.
- Measure before optimizing and compare runs only on equivalent environments.
- Do not maintain permanent old/new execution modes after parity is proven.
- Update architecture, contracts, operations, acceptance evidence, docstrings,
  and this roadmap when a boundary changes.

## Decisions needed only when their track starts

1. Which Windows workstation and fixture revisions are the scale-release
   reference?
2. Which real migration first requires clean-package certification, and who
   owns each scope-specific rule and reference dataset?
3. Which remote target, backup evidence, assurance triggers, and recovery
   expectations define the first production profile?
4. Which concrete business action, if any, justifies a target-side gateway?
5. Which deployment requirement, if any, justifies the hosted composition?
