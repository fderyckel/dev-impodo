# Security and infrastructure

## Purpose

This document summarizes Impodo's current security boundary, implemented
controls, infrastructure dependencies, and unresolved acceptance work. The
[Windows workstation requirements](../developer/setup/windows.md)
contain the detailed endpoint-provisioning checklist.

## At a glance

| Area | Current boundary |
| --- | --- |
| Application | Local Python process and browser UI bound to `127.0.0.1` |
| Recipe state | Bounded registry plus AES-256-GCM protected immutable Recipe and qualification payloads |
| DataVersion data | Owner-protected local files, immutable Parquet snapshots, and one contained DuckDB workspace |
| Source intake | Governed `.csv` and `.xlsx` only |
| Odoo reads | Fixed local reads or closed remote Odoo 19 JSON-2 `fields_get` and `search_read` |
| Odoo writes | Separate explicit local or remote JSON-2 load/create/update capability, bound to one reviewed preview |
| Execution evidence | Durable per-row journal plus hash-bound read-back reconciliation and fallout export |
| Current scale | 100,000 verified native-columnar direct rows; 50,000 Python-fallback direct rows; 25,000 derived/materialized rows |
| Hosted service | Not part of the current deployment |

## Trust boundaries

```text
Managed workstation

  managed browser
        |
        | random 127.0.0.1 HTTP port
        v
  Impodo / FastAPI
        |-- protected application root and Recipe registry
        |-- encrypted Recipe/qualification payloads
        |-- per-DataVersion DuckDB and immutable source/prepared snapshots
        |-- resource-bounded source-file worker
        |-- local credential vault
        |
        | fixed literal-loopback metadata/record reads, no API key
        | or outbound HTTPS read-only JSON-2 with a read account
        v
  authorised Odoo 19 target

  Separate explicit load path
        |
        | literal-loopback or outbound HTTPS Odoo 19 JSON-2
        | preview-scoped search_read / load / create / write
        | dedicated least-privilege API key
        v
  approved disposable local or remote Odoo database
```

Impodo has no public listener, inbound Odoo connection, application cloud
storage, telemetry service, direct PostgreSQL target access, or SSH surface.
It has no Odoo delete, arbitrary import, caller-selected method, generic RPC,
SQL, `sudo`, or workflow-action surface.

## Implemented controls

### Local browser

- Uvicorn binds an operating-system-selected port on literal `127.0.0.1`.
- A random single-use launch token is exchanged for a short-lived signed
  session and removed from the active URL.
- State changes require CSRF and same-origin validation; `GET` is not used for
  mutations.
- Host validation, restrictive response headers, framing denial, and
  `Cache-Control: no-store` reduce browser attack surface.
- API documentation, CORS, proxy forwarding, and access logs are disabled.
- Transformation-report filtering and CSV download use JavaScript shipped with
  Impodo. No CDN, browser extension, or Node.js runtime is required.
- Application services enforce fine-grained capabilities and retain stable
  actor identity in governed audit records. In the current single-user local
  composition, the authenticated local operator receives all capabilities;
  Windows account access, the launch session, and physical workstation control
  remain the user boundary. Hosted role assignment is not implemented.

Loopback uses HTTP, so the session cookie cannot use `Secure`. Literal
loopback binding, exact-host validation, the ephemeral port, and the launch
secret reduce but do not remove that limitation.

### Source files

- Intake accepts only bounded CSV and XLSX files and does not trust browser
  content types.
- Paths, signatures, Office containers, macros, external connections,
  embedded content, compression ratios, formulas, and size limits are checked.
- Validation and inspection run in spawned workers with time and memory
  limits.
- Files are copied under generated names through partial staging and atomic
  rename; display names are metadata only.
- Registered source bytes are SHA-256 hashed and remain immutable.
- Freezing parses each selected table once through that same strict reader and
  publishes a lossless, content-addressed Parquet snapshot below the owning
  DataVersion artifact root. Temporary fragments are contained and removed on
  every exit path; the completed file is schema/count/semantic/hash checked
  before atomic rename and DuckDB pointer publication.

Endpoint antivirus or content-disarm requirements remain an organizational
policy decision.

### Recipe and DataVersion storage

- Normal Windows storage is `%LOCALAPPDATA%\Impodo\projects` on a local drive.
- Impodo creates and verifies an owner-protected DACL before opening project
  data.
- On Windows, normal mode rejects Git checkouts, sync roots, network drives,
  symlinks, and junctions. Development mode is for synthetic or disposable
  data only.
- The protected root contains the bounded Recipe/DataVersion registry and
  encrypted protected Recipe store. Recipe, DataVersion, and workspace IDs are
  distinct and are resolved explicitly.
- Each DataVersion source package and each MigrationWorkspace use separate
  DuckDB stores behind application-owned repositories; users and mappings
  receive no SQL console. The workspace store keeps source references, not a
  copy of the accepted source package.
- Native-columnar preparation publishes a mapping-bound immutable prepared
  Parquet snapshot. Its manifest binds the exact source snapshot, mapping,
  schema, transformation program, row count, physical schema, and Parquet
  bytes. DuckDB advances its current pointer only after canonical publication
  succeeds.
- Snapshot paths are application-constructed from validated dataset and hash
  bindings. Traversal and symlink escapes are rejected, and Windows refuses a
  governed snapshot path longer than the 259 UTF-16-code-unit portable limit
  before filesystem access.
- DuckDB external access and extension autoload are disabled, and connections
  have bounded memory and threads.

Application-level encryption is implemented for immutable Recipe and
qualification payloads and for protected Odoo provenance, not for every
workspace artifact. Recipe payloads use AES-256-GCM with one Recipe-scoped key
kept in the operating-system vault. Odoo provenance uses the same primitive
with a project-scoped key and a narrow typed sidecar. Authenticated context
binds each payload, and exact ciphertext bytes are hash-verified at the
repository boundary and on read. Protected directories/files use private
`0700`/`0600` permissions where POSIX modes apply. Authorized services enforce
retained-history quota, expiry, invalidation, and key deletion during project
deletion. Numeric IDs, protected filters, principal/company identifiers, and
target-bound current/difference values use this restricted sidecar class.
Bulk source values remain one governed typed artifact under the project's
classification and are not copied into that sidecar. Existing portable project
evidence still depends on full-disk encryption and operating-system access
controls.

### Secrets

- Remote read API keys and disposable-target load API keys remain in memory or
  the local credential vault under separate read/write service labels.
- Stored credential identifiers and versioned envelopes are bound to the
  project, role, connection mode, URL, and database. Read-side routes retrieve
  only the read role; write and read-back routes retrieve only the write role,
  with no legacy or cross-role fallback.
- Model and schema evidence records a random, secret-independent read-
  credential-generation hash. It detects an Impodo-side credential rotation
  but is not represented as an Odoo principal or permission fingerprint.
- Remote identity probing is a fixed JSON-2 sequence: the API key's own
  `res.users/context_get`, one exact self-record `search_read`, one bounded
  active-company ID projection, and `has_access('read')` for service-selected
  models. Only principal, observed-permission, and context hashes leave the
  connector boundary. No user/group catalogue, company business fields, or
  caller-selected method is exposed.
- A separate remote write-identity connector first exercises that same closed
  self/context/read sequence for every model in the reviewed execution scope,
  then calls only `has_access('write')` for models with reviewed write fields.
  It performs no write and exposes only write-principal, observed-permission,
  and context hashes.
- Successful credential storage or replacement adds an actor-bound DuckDB
  audit event containing the random credential-generation binding and storage
  class. Secrets and raw Odoo identity values remain excluded.
- Target change and project deletion remove both role-qualified vault entries.
  Each entry that existed produces an actor-bound, non-secret registry receipt
  containing its binding hash when recoverable, storage class, target hash, and
  removal reason. The receipt survives project-directory deletion.
- Credentials are excluded from the registry, DataVersion and workspace
  databases, mappings, reports, browser storage, and logs.
- Credential generations are operational bindings to the exact target and
  run/workspace context. They are not DataVersion source evidence. Secrets
  never enter a Recipe revision, qualification, or cutover candidate, and Test
  credentials are never copied to Production.
- Authenticated redirects are refused; API keys and Odoo error bodies are
  redacted from public errors.

### Odoo access

The remote read connector requires HTTPS outside literal loopback and exposes
only Odoo 19 JSON-2 `fields_get`, `search_read`, and the fixed identity sequence
above. Reads are projected, batched by model, and paginated deterministically.
The separately confirmed writer described below does not widen that read
connector.

Local read mode uses the explicitly selected `odoo.conf` and fixed scripts for
the model catalogue, `fields_get`, and bounded `search_read`. It requires no
Odoo API key and is not a generic shell or RPC interface. Optional local-stack
controls use fixed argument lists and may stop only the exact Odoo/PostgreSQL
processes started by the current Impodo session.

Odoo-side defense in depth remains mandatory: a dedicated service user,
explicit ACLs and record rules, permitted-company context, model/field scope,
and governed key rotation and revocation.

### Controlled Test and qualified-plan Production execution

The writer is a separate adapter from every read connector. A load is allowed
only when all of these are true:

- the MigrationRun target binding is Local or Remote and identifies Odoo 19;
- the current immutable execution snapshot matches the page the operator
  reviewed and contains no blocked or ambiguous rows;
- the writer target hash matches the exact URL and database in that snapshot;
- the per-preview API scope matches the exact captured models, lookup fields,
  readable fields, and writable fields;
- no earlier run already consumed that snapshot; and
- a separate target-bound write-role credential is supplied or already stored;
  target setup may create it from the same explicitly approved secret, but the
  setup read role is never substituted during execution; and
- for remote execution, the write credential has read-back access to every
  scoped model and write access to every model with reviewed write fields, and
  its context matches the reviewed schema context; and
- the operator makes one explicit **Load into Odoo** request with the execute
  capability.

The closed JSON-2 surface permits exact-key `search_read`, remote External-ID
`load` batches or local `create` batches of at most 50 rows, and one uniquely
re-matched record per `write`. Request bodies are capped at 1 MiB. Standard,
extended, and custom models are supported only when they appear in the
captured schema and reviewed preview; there is no global model or field
permission that can widen a particular run.

Required-at-create relationship cycles block the preview before any write.
For an explicitly deferrable cycle, Impodo creates the records first and then
applies the exact reviewed relationship with one second ORM `write`. If that
patch is rejected, the row is recorded as partially applied; if its outcome is
unknown, later writes stop. In both cases the already-created Odoo ID remains
in the protected journal for reconciliation.

Every proposed write is journaled. A definitive rejection is recorded without
pretending the row committed. A lost or invalid write response becomes
**outcome unknown**, is never retried automatically, and stops later writes in
the run. Reconciliation reads committed rows by journaled ID, re-matches
uncertain outcomes by governed business key, compares reviewed fields, and
stores a hash-bound result with a downloadable fallout CSV. Only an uncertain
create proven absent is marked safe to plan again; updates are never declared
retry-safe merely because read-back could not find them.

For ordinary workspaces this remains a disposable-target migration
capability. Production authority exists only for an active Project Production
run that pins the current selected and authenticated CutoverPlan, a fresh
frozen Production DataVersion, a different Odoo 19 target, and exact current
read and write credential generations. The guard is repeated before writer
construction. Neither qualification nor selection alone authorizes a write.

This boundary does not authorize arbitrary Odoo business actions or direct
database writes. The reviewed API scope, execution snapshot, journal, and
read-back rules remain unchanged in Production.

The current Odoo-source round-trip policy still records native source-system
Production writes as `PRODUCTION_WRITE_UNSUPPORTED`. Impodo writes only through
fresh file-source Production applications after exact activation. JSON-2
proves the configured endpoint/database, not a restored or cloned database
instance, and separate read/write requests cannot provide an atomic
compare-and-write transaction.

## Infrastructure dependency

Normal use requires a managed and patched workstation, full-disk encryption,
EDR/antimalware, host firewall, a supported browser, approved Python 3.12,
writable local application/temp storage, and an accepted internal Impodo
bundle. Remote access additionally requires trusted TLS and governed
network/VPN routing. Remote reads need an evidenced read-only Odoo account. A
disposable local or remote load requires a separate least-privilege Odoo API
key whose ACLs, record rules, companies, and field access cover only the
reviewed rehearsal scope.

Excel review packages are generated locally with the controlled Python
`openpyxl` dependency already required for XLSX intake. Node.js is not an
Impodo workstation dependency.

The columnar preparation track pins Polars `1.43.2` and its matching
`polars-runtime-32` wheel in the authoritative Windows/Python 3.12 binary-only
lock. Polars is distributed under the OSI-approved MIT License; the wheel
retains its `LICENSE` metadata, and the internal release process records the
resolved package in dependency-audit and CycloneDX SBOM evidence. Polars now
executes only the application-constructed local Parquet ingestion/scan plan;
it receives no browser-controlled path or expression. DuckDB external access
remains disabled because snapshot bytes move through the artifact and
repository ports rather than DuckDB filesystem readers.

Impodo requires no inbound firewall opening. Detailed installation and
verification steps belong in the
[workstation runbook](../developer/setup/windows.md), not in
this security summary.

## Data handling

- Record the Recipe classification. The current browser bootstrap stores
  `INTERNAL`; do not treat that technical default as customer approval for
  confidential or restricted data.
- Keep sources, DuckDB data, snapshots, reports, and audit evidence only in
  their protected DataVersion or workspace roots below the Project boundary.
- Never place project data in Git, email, unauthorized shared storage, or
  unapproved sync/cloud locations.
- Keep raw sources immutable and retain evidence for derived corrections.
- Keep credentials outside DataVersion and workspace artifacts and remove
  related vault entries when the Project closes.
- Keep execution journals and reconciliation results inside the protected
  project boundary. They may contain target-specific Odoo record IDs; portable
  mappings, staging evidence, manifests, workbooks, and CSV reports must not.
- Retain project data through acceptance and reconciliation, then apply the
  data owner's documented retention, legal-hold, backup, and deletion rules.

SSD deletion assurance depends on encryption and key governance, not claims
of reliable per-file overwriting.

## Acceptance gaps

Before using real customer data or claiming production readiness, complete:

- an accepted, clean, evidence-producing internal release bundle;
- organization-specific workstation and protected-root verification;
- live Odoo read and writer ACL, record-rule, field-access,
  company-scope, and key-lifecycle proof;
- representative source-volume and sanitized live-target acceptance;
- the 100,000-row Windows repetition before making a cross-platform claim;
- an explicit decision on loopback HTTP and endpoint content scanning;
- customer-approved classification, retention, backup, legal hold, deletion,
  and support-access rules;
- any required threat model, privacy assessment, penetration test, and
  customer security review.

These gaps do not authorize broadening the read connector or deploying the
current writer as a production executor. The writer remains separate,
disposable-target-only, and bound to the exact captured-schema fields in one
reviewed preview. Remote capability is not production authorization.

## Evidence and references

Project evidence:

- [Product vision and current delivery boundary](../product-vision.md)
- [Architecture overview](overview.md)
- [Python code map and execution boundary](python-code-map.md)
- [Recipe and data-version lifecycle contract](../developer/contracts/recipe-lifecycle.md)
- [Contained project lifecycle contract](../developer/contracts/project-lifecycle.md)
- [Preflight contract](../developer/contracts/preflight.md)
- [Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md)
- [Acceptance and test strategy](../testing/acceptance.md)
- [Internal release runbook](../developer/runbooks/internal-release.md)
- [Windows workstation requirements](../developer/setup/windows.md)

External control references:

- [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [DuckDB security overview](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)
- [Odoo 19 External JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html)
