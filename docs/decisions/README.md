# Architecture decisions

These decisions are accepted for the read-only milestone. Reversing one
requires an explicit architecture decision update and review of affected
contracts.

## ADR-001 — Prepared records are the portable domain boundary

**Status:** Accepted  
**Decision:** Source adapters produce immutable, typed,
target-independent `PreparedRecord` objects with structured issues.
Comparison consumes prepared records, never raw source rows.

**Why:** The old shape checked values without retaining typed mappings.
Keeping parsing and comparison separate prevents repeated conversion and makes
fixture testing independent of spreadsheets.

**Consequences:**

- type and normalization behavior must be complete before target comparison;
- raw source values are confined to source diagnostics;
- prepared-record shape changes require coordinated fixture and artifact
  regeneration during the proof of concept;
- no Odoo ID can be used to make an otherwise incomplete prepared record
  valid.

## ADR-002 — Target evidence is captured in immutable snapshots

**Status:** Accepted  
**Decision:** Target metadata and records are captured as separate,
content-addressed, target-specific snapshots. Comparison can run entirely
offline from those files.

**Why:** It separates live connectivity from domain correctness, makes tests
repeatable, and binds a review result to exact target evidence.

**Consequences:**

- snapshots may contain Odoo IDs and must be protected as environment data;
- snapshot completeness and hashing are mandatory;
- a preflight result becomes stale when the target changes and must not be
  presented as current without a new snapshot;
- fixture and live connectors must return equivalent normalized data.

## ADR-003 — The connector is read-only by capability

**Status:** Accepted  
**Decision:** `OdooReadConnector` exposes fingerprint, metadata, and record
catalog reads only. There is no generic RPC method.

**Why:** Naming a class "read-only" is not a security boundary if it can call
arbitrary model methods. A narrow interface makes accidental writes impossible
through normal application code and makes the milestone auditable.

**Consequences:**

- unusual reads must be expressed as explicit request types, not escape
  hatches;
- live credentials still require Odoo-level read-only ACLs;
- a future executor uses a separate interface, package, configuration, and
  security review.

## ADR-004 — Relations compare by natural identity

**Status:** Accepted  
**Decision:** Prepared references and portable differences use target model,
ordered natural identity, and natural scope. Snapshot relation IDs are
reverse-resolved before comparison.

**Why:** Numeric IDs vary between fixtures and Odoo databases. Comparing them
or approving them would make the plan target-dependent.

**Consequences:**

- reference catalogs require bidirectional indexes;
- unresolved target IDs block affected records;
- scoped and composite reference identities must be supported from the start;
- report and manifest serializers reject Odoo ID keys recursively.

## ADR-005 — Classification fails closed with fixed precedence

**Status:** Accepted  
**Decision:** The only row classifications are `CREATE`, `UPDATE`,
`UNCHANGED`, `AMBIGUOUS`, and `BLOCKED`, evaluated in this order:

```text
blocking issue?
  yes → BLOCKED
  no  → target matches > 1?
          yes → AMBIGUOUS
          no  → target matches = 0?
                  yes → CREATE
                  no  → differences?
                          yes → UPDATE
                          no  → UNCHANGED
```

**Why:** A complete and deterministic outcome model is necessary for
reconciliation and review. Uncertain evidence must never imply a create or
update.

**Consequences:**

- ambiguous target identity is a classification;
- ambiguous relation resolution is a blocking issue because target matching
  cannot safely begin;
- incomplete target snapshots stop the run rather than classifying rows;
- all rows in non-reference datasets must reconcile to exactly one outcome.

## ADR-006 — Canonical serialization defines reproducibility

**Status:** Accepted  
**Decision:** Domain values have canonical JSON forms, arrays have declared
stable ordering, and source/snapshot inputs plus outputs are hashed.

**Why:** Semantic repeatability alone is difficult to audit. Canonical
serialization allows fixtures to prove byte-level stability and lets reviewers
bind a decision to exact evidence.

**Consequences:**

- decimals use typed lossless strings and integers use JSON integers;
- the snapshot timestamp is part of the target fingerprint and therefore
  part of the semantic hash;
- the manifest adds no separate generated timestamp or run ID;
- profile identity is hashed through the manifest, but the proof of concept
  does not hash the profile file bytes;
- output writers do not depend on hash-map iteration or locale;
- engine changes may intentionally change hashes.

## ADR-007 — The requirements plan precedes connector access

**Status:** Accepted  
**Decision:** Profile compilation and prepared natural keys produce a
deterministic tuple of `MetadataRequest` or `RecordRequest` values. Connectors
accept only requests derived by those planner functions.

**Why:** This enforces data minimization, enables request auditing, and ensures
fixture and live execution ask the same questions.

**Consequences:**

- requests have deterministic ordering, but the proof of concept does not yet persist
  a requirements-plan hash in snapshots;
- the metadata plan can be built before source preparation, while bounded
  record domains are finalized after prepared identities are known;
- single-field identities produce bounded `in` domains; composite identities
  can require a broader profile-domain read;
- snapshots record exact projected fields, but not the requested domain.

## ADR-008 — Local and hosted deployments use separate composition roots

**Status:** Accepted
**Decision:** Impodo keeps one portable domain and application-service layer
with two explicit deployment profiles:

- the local profile uses a loopback launch session, DuckDB, contained local
  artifacts, Windows Credential Manager, and synchronous jobs;
- the future hosted profile uses corporate identity, centrally governed
  authorization, PostgreSQL, shared artifact storage, durable workers, and a
  TLS reverse proxy.

The local security middleware is not relaxed to create the hosted profile.
Hosted HTTP, identity, persistence, secrets, and job adapters are composed
separately.

Application services receive verified actors and depend on ports for
authorization, project persistence, artifacts, secrets, and jobs. Immutable
approval evidence binds decisions to stable actor identities and exact input
hashes. DuckDB may remain a worker-local analytical engine, but it is not the
hosted multi-user system of record.

**Why:** Containerizing the current local process would preserve its
single-user launch token, filesystem, keyring, and single-process DuckDB
assumptions. Explicit adapters let the MVP remain small while preventing the
mapping, normalization, approval, and audit domains from depending on those
assumptions.

**Consequences:**

- local behavior and its loopback protections remain the default;
- every state-changing project command carries a verified actor and audit
  identity;
- source processing uses storage keys and materialization rather than
  repository-owned paths;
- long-running work has an idempotent job contract even when the local adapter
  executes synchronously;
- approval status is only a derived summary; immutable decision and approval
  records are authoritative;
- PostgreSQL, SSO, hosted Docker deployment, and the restricted Odoo executor
  remain separate delivery and security milestones;
- contract tests must run against each future repository, artifact, identity,
  authorization, and job adapter.

## ADR-009 — Odoo source round trips are target-bound and update-only

**Status:** Accepted

**Decision:** An Odoo-source row may round-trip only to the same configured
target and original protected record identity. Missing records block; there is
no business-key or create fallback. Numeric IDs remain in separately authorized
protected evidence and never enter portable mappings, rows, reports, or
execution snapshots.

**Consequences:**

- source capture, preparation, comparison, and execution bind one policy hash;
- refresh creates new evidence and invalidates dependent current pointers;
- the current file and Odoo source representations have no compatibility
  decoder or database upgrade path.

## ADR-010 — Native JSON-2 production writes are unsupported

**Status:** Accepted

**Decision:** The current native Odoo 19 JSON-2 profile provides
connection-only identity assurance. Endpoint, mode, and database name cannot
distinguish a restored or cloned database, and independent JSON-2 read/write
requests cannot implement Impodo's required atomic compare-and-write
transaction. The executable policy therefore records
`PRODUCTION_WRITE_UNSUPPORTED`.

**Consequences:**

- bounded read-only Odoo-source capture may proceed;
- existing write support remains explicitly disposable-target capability;
- production enablement requires a new current architecture with strong
  instance identity and one server-side atomic operation, not a hidden fallback.

## ADR-011 — Target-bound Odoo provenance is restricted evidence

**Status:** Accepted

**Decision:** Numeric Odoo IDs, protected filters, principal/company
identifiers, and target-bound current/difference values are classified
`RESTRICTED_TARGET_EVIDENCE`. Application-level encryption is required before
that sidecar evidence is persisted. Bulk captured source values remain one
typed source artifact under the project's data classification and existing
private artifact controls; they are not copied into the protected sidecar. The
sidecar is excluded from backups unless explicitly approved and is deleted on
project deletion or retention expiry.

**Consequences:**

- full-disk encryption alone is insufficient for the protected store;
- the protected provenance repository uses project-scoped AES-256-GCM keys in
  the operating-system vault, authenticated manifest bindings, private paths,
  authorization, retained-history quota, retention, invalidation, and deletion;
- encryption must not create a second copy of the wide typed source values;
- credential removal produces actor-bound, non-secret registry receipts that
  survive project deletion.

## ADR-012 — Project series group contained migration projects

**Status:** Accepted

**Decision:** A reusable business migration is represented by a new
`ProjectSeries` aggregate above existing `MigrationProject` workspaces. Each
data version remains a complete contained migration project with its own
`project_id`, DuckDB database, artifacts, credentials, evidence, and audit
boundary. The series has an independently generated `series_id`, owns the
edition lineage and recipe history, and never substitutes its identifier for a
project identifier.

Only one registered edition is active. A newly allocated workspace is pending
until normal project registration succeeds; it does not replace the current
registered edition. Activation makes the new edition current and seals the
prior edition. Sealing is enforced by series-aware application policy and a
project-local marker, not only by browser presentation.

Reusable recipe configuration belongs to a protected series-scoped store.
Registry rows contain bounded series, edition, lifecycle, hash, intent, and
read-model metadata, not confidential recipe payloads or edition evidence.

**Why:** The existing `MigrationProject` is already the authorization,
credential, filesystem-containment, lifecycle, and immutable-evidence boundary.
Redefining it or adding a data-version discriminator to every project table
would weaken those contracts. Cloning a project database would copy stale
current pointers and evidence. A separate aggregate preserves the contained
workspace while making portable business meaning reusable.

**Consequences:**

- backfill generates a new series UUID; `series_id = project_id` is forbidden;
- routes, authorization, credentials, repositories, deletion, and logs keep
  series and project scopes explicit and reject identifier confusion;
- series-owned setup is a field-level allowlist and legacy hydration is lazy,
  authorized, hash-bound, and unavailable to recipe actions until complete;
- pending, active, sealed, abandoned, and deleting states have explicit
  transitions and restart-safe recovery;
- edition creation, recipe publication, persistent read-credential copy, and
  whole-series deletion use idempotent intents/outboxes across stores;
- only the active or pending edition may accept the mutations allowed by its
  state; a sealed edition remains reopenable but cannot become current evidence;
- full recipe payloads inherit series classification, retention, backup,
  authorization, and deletion policy in a protected store; and
- a recipe is execution-engine-neutral configuration, never a source, target,
  validation, approval, comparison, or execution snapshot.

The frozen proposed contract and acceptance examples are recorded in
[Reusable recipe Phase 0 contracts](../plans/reusable-recipes-phase-0-contracts.md).
