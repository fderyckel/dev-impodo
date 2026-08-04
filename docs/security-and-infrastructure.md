# Impodo security and infrastructure

This document explains what has been developed, how it is protected, what the
surrounding environment must provide, and which capabilities do not yet exist.
It is an informational description. The separate
[Windows workstation readiness checklist](operations/windows-workstation-readiness.md)
translates these controls into endpoint-preparation actions.

## At a glance

| Area | Current state |
| --- | --- |
| Application | Local Python process with a browser interface bound to `127.0.0.1` |
| Source data | Governed `.csv` and `.xlsx` intake with local DuckDB storage |
| Odoo access | Read-only access to authorised Odoo 19 targets |
| Lifecycle stage | Defined by the project, not by Impodo |
| Odoo writes | No create, update, delete, import, or generic method capability |
| Hosted/cloud service | Not yet part of the current local deployment |



## Architecture and trust boundaries

```text
Managed Windows workstation

  Managed browser
        |
        | HTTP on random 127.0.0.1 port only
        v
  Impodo / FastAPI
        |-- governed CSV/XLSX intake and isolated inspection
        |-- per-project DuckDB and immutable source copies
        |-- Windows Credential Manager for optional API keys
        |
        | outbound HTTPS over company-controlled network/VPN
        v
  authorised Odoo 19 target
        dedicated read-only service user
```

- Impodo is a local Python process with a server-rendered browser interface.
- It has no public listener, CDN, telemetry service, or application cloud
  storage in the current boundary.
- Not yet implemented: No inbound connection from Odoo, direct PostgreSQL access, SSH access.


## Current product boundary

Implemented in the current workspace:

- authenticated project setup, source inspection, dataset freezing, Odoo
  schema capture, governed business keys, mapping, transformations, semantic
  validation, immutable revisions, and exact-hash submission evidence;
- local-versus-remote Odoo modes and a deliberately closed read surface;
- owner-protected Windows project storage;
- a hash-locked Python 3.12 dependency file and internal release tooling.

Not implemented:

- governed lookup translations, mapping import/export, and complete functional
  review and authorization workflow;
- a signed end-user installer or an accepted internal release bundle;
- a pinned disposable Odoo/PostgreSQL Compose laboratory;
- organisation-specific lifecycle classification or promotion enforcement;
- Odoo create, update, delete, import, reconciliation, or other write
  execution.

Any future write capability requires a separate connector, process,
credential, frozen authorized plan, model/field allowlists, dependency
ordering, idempotency journal, operator confirmation, and independent security
design and verification. A read credential must never be silently upgraded to
write capability.

## Implemented application controls

### Local browser

- Uvicorn binds an operating-system-selected port on literal `127.0.0.1`;
  `0.0.0.0` and LAN listeners are not used.
- The exact generated `Host` is allowlisted; proxy and forwarded-host headers
  are rejected and CORS is not enabled.
- A cryptographically random, single-use launch token is exchanged for a
  signed session and removed from the active URL.
- The session cookie contains no customer data or credentials and is
  `HttpOnly`, `SameSite=Strict`, and limited to 30 minutes.
- State-changing forms require CSRF tokens, exact same-origin
  `Origin`/`Referer` validation, and non-cross-site Fetch Metadata. `GET` is not
  used for state changes.
- Restrictive CSP, framing denial, MIME-sniffing prevention, permissions,
  opener/referrer, and `Cache-Control: no-store` headers are applied.
- API documentation endpoints and Uvicorn access logs are disabled; server
  concurrency and keep-alive are bounded.

The loopback site uses HTTP, so its cookie cannot use the `Secure` attribute.
Literal loopback binding, the ephemeral port, exact-host validation, and the
single-use launch secret reduce this exposure. If company policy requires
`Secure` on localhost, use locally trusted TLS or a separately reviewed native
wrapper.

### CSV/XLSX intake

- Only `.csv` and `.xlsx` are accepted. Legacy, macro-enabled, encrypted,
  arbitrary ZIP, symlink, device, URL, and network-path inputs are rejected.
- File extension, signature, Office container, archive paths, compression
  ratios, XML safety, external links/connections, macros, and embedded objects
  are checked rather than trusting the browser content type.
- Source files are copied under generated identifiers through partial staging
  and atomic rename; original names are display metadata only.
- Raw source bytes are SHA-256 hashed and remain immutable after registration.
- File, archive, worksheet, row, column, string, and expanded-size limits are
  enforced.
- Validation runs in a spawned worker with a 30-second timeout and 512 MiB
  memory limit; inspection uses a 60-second timeout.
- Formula cells are inventoried during inspection and rejected before strict
  source preparation. Macros, external connections, and embedded active
  content are never executed.

Endpoint antivirus or content-disarm integration remains a company-policy
decision before real data is accepted.

### Project storage and DuckDB

- Normal Windows storage is `%LOCALAPPDATA%\Impodo\projects` on a local drive.
- At startup Impodo creates and verifies a protected DACL granting access only
  to the current user, `SYSTEM`, and local Administrators.
- Normal mode rejects Git checkouts, OneDrive roots, network drives, symlinks,
  and junctions. An explicit development mode may relax this only for
  synthetic or disposable data.
- Each project uses a separate DuckDB database behind application-owned
  repositories; there is no SQL console or mapping-supplied SQL.
- DuckDB external access, community extensions, extension autoinstall/autoload,
  and later configuration changes are disabled. Connections are limited to
  256 MiB and two threads.
- Project identifiers are UUIDs and stored paths are resolved below governed
  roots.

Application-level project encryption is not implemented. Confidentiality at
rest depends on BitLocker and Windows access controls.

### Secrets

- Remote Odoo API keys are held in memory or Windows Credential Manager.
- Stored credential identifiers are bound to the exact project, connection
  mode, URL, and database.
- The application excludes credentials from project databases, mappings,
  manifests, reports, browser storage, and logs. Operators must not place them
  in command history, Git, or `.env` files.
- API keys and Odoo error bodies are redacted from public errors and object
  representations; authenticated redirects are refused.
- Local Windows Odoo metadata discovery does not require an API key.

### Odoo read-only boundary

Remote mode:

- requires a non-loopback HTTPS target;
- dispatches only Odoo 19 JSON-2 `fields_get` and `search_read`;
- refuses redirects before bearer credentials can be forwarded;
- projects and paginates target reads deterministically.

Local mode:

- accepts HTTP only for literal `127.0.0.1` or `::1` Odoo targets;
- uses the explicitly selected `odoo.conf` and fixed, bounded model-catalogue
  or `fields_get` operations without a generic shell/RPC surface;
- reads `ir.model` once for discovery and calls `fields_get` once per selected
  model, avoiding per-field N+1 requests;
- rolls back the Odoo transaction and stores normalized, hash-bound snapshots
  for later offline mapping.

There is no `create`, `write`, `unlink`, import, server action, `execute_kw`,
SQL, or generic method-call interface. Odoo-side defense in depth is still
required: a dedicated service user, explicit model ACLs and record rules,
permitted-company context, model/field allowlists, expiring API keys, and
rotation/revocation procedures.

### Local Odoo service lifecycle

The optional Windows readiness assistant is a machine-management boundary:

- it reads only allowlisted, non-secret routing values from the selected
  `odoo.conf` and keeps paths/session details in process memory;
- readiness probes and startup use fixed executable argument lists without an
  operating-system shell;
- startup requires explicit confirmation and PostgreSQL readiness before Odoo;
- Impodo records ownership only for the exact Odoo child and PostgreSQL
  instance it started;
- stop/restart requires explicit capability and confirmation, stops Odoo first,
  verifies the port, and checks the retained PostgreSQL PID before a bounded
  `pg_ctl stop -m fast`;
- externally started services are status-only and are never terminated.

Ownership is not persisted across Impodo sessions. The user must stop managed
services before quitting. A PostgreSQL fast stop can disconnect another local
tool that began using that Impodo-started server, so the UI identifies managed
services and requires confirmation.

## Infrastructure requirements

The workstation must be company-owned and managed, with:

- a supported, patched Windows version and named standard-user account;
- BitLocker with governed recovery keys, screen lock, EDR/antimalware, and host
  firewall;
- company-managed 64-bit CPython 3.12, dependency source, and browser;
- writable local `%LOCALAPPDATA%` and `%TEMP%` locations;
- project storage outside Git, email, network/removable drives, and sync
  folders;
- an explicit backup decision matching classification and deletion duties.

Remote Odoo additionally requires:

- company-controlled LAN/VPN routing and a TLS chain trusted by Python;
- exact URL and database-routing name;
- a dedicated read-only Odoo service user with evidenced ACLs, record rules,
  company scope, and model/field access;
- documented API-key issue, expiration, rotation, revocation, and incident
  procedures.

No inbound firewall opening is required for Impodo.

## Data protection and retention

The implemented project model records classification and retention. The
following operating defaults remain subject to company and customer policy:

- record project classification; default to `CONFIDENTIAL`;
- keep source exports, DuckDB data, snapshots, reports, and audit artifacts
  only in the encrypted, owner-protected local project directory;
- disable support access by default and authorize it explicitly when needed;
- never place project data in Git, email, shared drives, telemetry, or
  unauthorized sync/cloud storage;
- keep raw sources immutable; derived corrections retain raw and canonical
  values, rule/reason, operator, timestamp, and mapping/rule version;
- keep credentials outside project artifacts and delete related credential
  entries when the project closes;
- retain the complete project through reconciliation and acceptance, then 90
  days unless the data owner documents another period;
- at expiry, delete project files and exported copies and record non-sensitive
  deletion evidence.

On SSDs, deletion assurance depends on full-disk encryption and key governance,
not claims of reliable per-file overwriting. The organization and data owner
must define classification, retention, backup, legal hold, deletion, and
support-access rules for the data being processed.

## Release and verification evidence

Verified locally on Windows on 3 August 2026 after the target-fingerprint
refactor:

- Python suite in bounded groups: **141 tests run, 140 passed, one optional workbook test
  skipped, no failures**;
- focused target-contract regression: **54 tests passed**;
- Windows project-root security suite: protected DACL creation/verification and
  unsafe-root rejection passed;
- `requirements.windows-py312.lock`: exact Python 3.12 dependency pins,
  SHA-256 hashes, and binary-only policy validated;
- non-release mechanics check: clean runtime installation, `pip check`,
  installed-wheel CLI smoke test, dependency audit with no known
  vulnerabilities, and validated CycloneDX SBOM passed.

The target-fingerprint refactor is complete across callers, fixtures, browser
flows, contracts, and tests. The optional workbook integration remains an
optional-tooling gate because Node.js is not installed on this workstation. The
release gate also refuses the current dirty worktree.

The tests cover the loopback session boundary, Host/origin/CSRF controls,
security headers, local/remote URL separation, credential rebinding, DuckDB
locks, isolated file validation, schema capture, mapping invalidation,
read-connector closure, local service ownership, API-key redaction, target
pagination, portable-ID rejection, deterministic artifacts, and fail-closed
classification.

Internal release automation is present and is designed to require a clean Git
revision, a fresh source snapshot, the full tests, secret scan, hash-locked
dependency installation, allowlisted wheel contents, vulnerability audit,
reproducible SBOM, clean-runtime install, and SHA-256 release manifest. Its
existence is not release evidence: an end-to-end bundle has not yet been
produced, signed/allowlisted, and accepted.

The skipped workbook test requires the optional Node.js/artifact-tool runtime.
If workbook generation is included in the pilot, that pinned runtime and test
become mandatory. This engineering evidence is not a penetration test or live
Odoo/infrastructure acceptance.

## Known limitations and remaining work

Product and release work still outstanding:

- produce and verify an exact internal release bundle; no signed end-user
  installer or accepted enterprise-allowlisted bundle exists yet;
- complete the 100-300-record sanitized acceptance slice and expected-scale
  resource tests;
- install and verify the optional workbook runtime if workbook generation is
  included in a deployment;
- harden and schema-validate saved snapshot envelopes and complete their
  tamper/failure tests;
- build the separately designed disposable Compose laboratory if it is needed.

Environment-specific validation is not product functionality and has not been
performed by the local automated suite:

- confirm the Windows, BitLocker, EDR, firewall, browser, Python, and storage
  baselines on the employee's actual account;
- determine how company policy treats loopback HTTP without a `Secure` cookie;
- determine whether endpoint antivirus is sufficient for CSV/XLSX intake or
  whether content-disarm scanning is required;
- define classification, retention, backup, legal hold, deletion, and support
  access for the intended data;
- verify the protected DACL on the exact installed release and laptop;
- verify live Odoo ACLs, record rules, company scope, TLS/VPN routing, API-key
  rotation/revocation, and unchanged sentinel `write_date` evidence;
- perform any threat model, privacy assessment, penetration test, or customer
  review required by the organization.

A future Odoo write executor remains a separate architecture and security
workstream.

## Evidence and primary references

Project evidence:

- [Migration project contract](contracts/migration-project.md)
- [Read connector contract](contracts/read-connector.md)
- [Acceptance and test strategy](testing/acceptance.md)
- [Windows workstation readiness](operations/windows-workstation-readiness.md)
- [Python dependencies](../pyproject.toml)
- [Locked Windows dependencies](../requirements.windows-py312.lock)

External control references:

- [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [OWASP Content Security Policy Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html)
- [DuckDB security overview](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)
- [Odoo 19 External JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html)
- [Python ZIP-file decompression warning](https://docs.python.org/3/library/zipfile.html#decompression-pitfalls)
