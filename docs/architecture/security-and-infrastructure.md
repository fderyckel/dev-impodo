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
| Odoo access | Read-only access to authorised Odoo 19 targets |
| Odoo writes | No create, update, delete, import, or generic method capability |
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
        | outbound HTTPS, or literal-loopback local access
        v
  authorised Odoo 19 target
        dedicated read-only access
```

Impodo has no public listener, inbound Odoo connection, application cloud
storage, telemetry service, direct PostgreSQL target access, or SSH surface.

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

- Remote Odoo API keys remain in memory or the local credential vault.
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

Local mode uses the explicitly selected `odoo.conf` and fixed scripts for the
model catalogue and `fields_get`. It is not a generic shell or RPC interface.
Optional local-stack controls use fixed argument lists and may stop only the
exact Odoo/PostgreSQL processes started by the current Impodo session.

Odoo-side defense in depth remains mandatory: a dedicated service user,
explicit ACLs and record rules, permitted-company context, model/field scope,
and governed key rotation and revocation.

## Infrastructure dependency

Normal use requires a managed and patched workstation, full-disk encryption,
EDR/antimalware, host firewall, a supported browser, approved Python 3.12,
writable local application/temp storage, and an accepted internal Impodo
bundle. Remote targets additionally require trusted TLS, governed network/VPN
routing, and an evidenced read-only Odoo account.

Excel review packages are generated locally with the controlled Python
`openpyxl` dependency already required for XLSX intake. Node.js is not an
Impodo workstation dependency.

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
- Retain project data through acceptance and reconciliation, then apply the
  data owner's documented retention, legal-hold, backup, and deletion rules.

SSD deletion assurance depends on encryption and key governance, not claims
of reliable per-file overwriting.

## Acceptance gaps

Before using real customer data or claiming production readiness, complete:

- an accepted, clean, evidence-producing internal release bundle;
- organization-specific workstation and protected-root verification;
- live Odoo ACL, record-rule, company-scope, TLS/VPN, and key-lifecycle proof;
- representative source-volume and sanitized live-target acceptance;
- an explicit decision on loopback HTTP and endpoint content scanning;
- customer-approved classification, retention, backup, legal hold, deletion,
  and support-access rules;
- any required threat model, privacy assessment, penetration test, and
  customer security review.

These gaps do not authorize broadening the connector. A future Odoo writer is
a separate architecture and security workstream.

## Evidence and references

Project evidence:

- [Architecture overview](overview.md)
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
