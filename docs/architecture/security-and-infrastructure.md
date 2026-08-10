# Security and infrastructure

## Purpose

This document summarizes Impodo's current security boundary, implemented
controls, infrastructure dependencies, and unresolved acceptance work. The
[Windows workstation requirements](../operations/05-windows-workstation-readiness.md)
contain the detailed endpoint-provisioning checklist.

## At a glance

| Area | Current boundary |
| --- | --- |
| Application | Local Python process and browser UI bound to `127.0.0.1` |
| Project data | Owner-protected local files and per-project DuckDB |
| Source intake | Governed `.csv` and `.xlsx` only |
| Odoo reads | Fixed local reads or closed remote Odoo 19 JSON-2 `fields_get` and `search_read` |
| Odoo writes | Separate explicit local or remote JSON-2 create/update capability, bound to one reviewed preview |
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
        |-- protected project root
        |-- per-project DuckDB and immutable source copies
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
  publishes a lossless, content-addressed Parquet snapshot below the protected
  project directory. Temporary fragments are contained and removed on every
  exit path; the completed file is schema/count/semantic/hash checked before
  atomic rename and DuckDB pointer publication.

Endpoint antivirus or content-disarm requirements remain an organizational
policy decision.

### Project storage

- Normal Windows storage is `%LOCALAPPDATA%\Impodo\projects` on a local drive.
- Impodo creates and verifies an owner-protected DACL before opening project
  data.
- On Windows, normal mode rejects Git checkouts, sync roots, network drives,
  symlinks, and junctions. Development mode is for synthetic or disposable
  data only.
- Each project uses a separate DuckDB database behind application-owned
  repositories; users and mappings receive no SQL console.
- DuckDB external access and extension autoload are disabled, and connections
  have bounded memory and threads.

Application-level project encryption is not implemented. Confidentiality at
rest depends on full-disk encryption and operating-system access controls.

### Secrets

- Remote read API keys and disposable-target load API keys remain in memory or
  the local credential vault.
- Stored credential identifiers are bound to the project, connection mode,
  URL, and database.
- Credentials are excluded from project databases, mappings, reports, browser
  storage, and logs.
- Authenticated redirects are refused; API keys and Odoo error bodies are
  redacted from public errors.

### Odoo access

Remote mode requires HTTPS outside literal loopback and exposes only Odoo 19
JSON-2 `fields_get` and `search_read`. Reads are projected, batched by model,
and paginated deterministically.

Local read mode uses the explicitly selected `odoo.conf` and fixed scripts for
the model catalogue, `fields_get`, and bounded `search_read`. It requires no
Odoo API key and is not a generic shell or RPC interface. Optional local-stack
controls use fixed argument lists and may stop only the exact Odoo/PostgreSQL
processes started by the current Impodo session.

Odoo-side defense in depth remains mandatory: a dedicated service user,
explicit ACLs and record rules, permitted-company context, model/field scope,
and governed key rotation and revocation.

### Controlled disposable-target execution and reconciliation

The writer is a separate adapter from every read connector. A load is allowed
only when all of these are true:

- the project target mode is Local or Remote and the captured target is Odoo
  19;
- the current immutable execution snapshot matches the page the operator
  reviewed and contains no blocked or ambiguous rows;
- the writer target hash matches the exact URL and database in that snapshot;
- the per-preview API scope matches the exact captured models, lookup fields,
  readable fields, and writable fields;
- no earlier run already consumed that snapshot; and
- the operator makes one explicit **Load into Odoo** request with the execute
  capability.

The closed JSON-2 surface permits exact-key `search_read`, create batches of at
most 50 rows, and one uniquely re-matched record per `write`. Request bodies
are capped at 1 MiB. Standard, extended, and custom models are supported only
when they appear in the captured schema and reviewed preview; there is no
global model or field permission that can widen a particular run.

Every proposed write is journaled. A definitive rejection is recorded without
pretending the row committed. A lost or invalid write response becomes
**outcome unknown**, is never retried automatically, and stops later writes in
the run. Reconciliation reads committed rows by journaled ID, re-matches
uncertain outcomes by governed business key, compares reviewed fields, and
stores a hash-bound result with a downloadable fallout CSV. Only an uncertain
create proven absent is marked safe to plan again; updates are never declared
retry-safe merely because read-back could not find them.

This is a disposable-target migration capability, not authorization for a
production cutover, arbitrary Odoo business actions, or direct database
writes.

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
[workstation runbook](../operations/05-windows-workstation-readiness.md), not in
this security summary.

## Data handling

- Record the project classification; default to `CONFIDENTIAL`.
- Keep sources, DuckDB data, snapshots, reports, and audit evidence only in
  the protected project directory.
- Never place project data in Git, email, unauthorized shared storage, or
  unapproved sync/cloud locations.
- Keep raw sources immutable and retain evidence for derived corrections.
- Keep credentials outside project artifacts and remove related vault entries
  when the project closes.
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
- [Migration project contract](../contracts/01-migration-project.md)
- [Profile-driven preflight contract](../contracts/04-preflight.md)
- [Acceptance and test strategy](../testing/acceptance.md)
- [Internal release runbook](../operations/06-internal-release.md)
- [Windows workstation requirements](../operations/05-windows-workstation-readiness.md)

External control references:

- [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [DuckDB security overview](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)
- [Odoo 19 External JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html)
