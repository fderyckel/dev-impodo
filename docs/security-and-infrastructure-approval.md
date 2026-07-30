# Impodo security and infrastructure approval brief

| Item | Value |
| --- | --- |
| Document status | Draft for cybersecurity and infrastructure review |
| Review scope | First-release local architecture through delivery Phase 2B implementation |
| Target platform | Managed Windows workstation; Odoo 19.4 DEV/TEST |
| Planned target change | Odoo 20.0 in September, subject to separate compatibility validation |
| Product owner for migration approval | Data manager |
| Document date | 29 July 2026 |

Delivery Phase 2C.1 scalar providers and allowlisted transformations were implemented
after this draft. They remain outside this document's reviewed approval scope
until the brief is refreshed and re-reviewed.

## 1. Approval requested

Approve the following architecture for continued implementation and a
sanitized, non-production pilot:

- a local-only browser application on the data manager's managed workstation;
- local project storage in DuckDB;
- controlled ingestion of exported `.csv` and `.xlsx` files;
- outbound, read-only access to approved Odoo DEV/TEST environments;
- no cloud service, inbound Odoo connection, production target, or Odoo write
  capability in the current release boundary.

This request does **not** seek approval for real customer-data processing,
production Odoo access, or an Odoo write executor. Those require the evidence
and controls listed in section 9.

## 2. Architecture and trust boundaries

```text
Managed Windows workstation

  Browser
    |
    | HTTP to a random port on 127.0.0.1 only
    v
  Impodo / FastAPI
    |-- project governance and audit metadata
    |-- source intake --> isolated validation worker
    |-- per-project DuckDB + immutable source copies
    |-- Windows Credential Manager for Odoo API keys
    |
    | outbound HTTPS over approved network/VPN
    v
  Odoo 19.4 DEV or TEST
    dedicated read-only service user
```

| Boundary | Design |
| --- | --- |
| User interface | Server-rendered local browser UI; no public web server, CDN, React, Electron, or browser-side customer-data store |
| Local service | Python/Uvicorn binds an OS-selected port on literal `127.0.0.1`; proxy headers and LAN binding are disabled |
| Untrusted files | `.csv`/`.xlsx` files are copied under generated names and validated in a spawned process with a 30-second timeout and 512 MiB memory limit |
| Project data | One governed project directory and DuckDB database per migration; raw intake is immutable and SHA-256 hashed |
| Secrets | Odoo API keys are held in process memory or Windows Credential Manager; they are excluded from DuckDB, manifests, reports, browser storage, and logs |
| Odoo | Outbound read-only calls to `/web/version` plus allowlisted JSON-2 methods; local HTTP is allowed solely for a literal loopback Odoo DEV instance, while remote targets require HTTPS |

There is no required inbound connection from Odoo, direct PostgreSQL access,
SSH access, Odoo master password, generic RPC surface, or arbitrary SQL/Python
execution.

## 3. Current implementation boundary

Implemented:

- authenticated project-setup browser;
- loopback-only launch and session boundary;
- governed project metadata, source evidence, registration manifest, and audit
  event;
- per-project DuckDB persistence with locked security settings;
- Windows Credential Manager integration with target-bound credential IDs;
- isolated and resource-bounded CSV/XLSX container validation;
- explicit local-versus-remote Odoo connection modes;
- narrow read connector exposing environment fingerprint, `fields_get`, and
  `search_read` only;
- deterministic offline preflight and review artifacts.
- hash-bound CSV/XLSX inventory, interactive parsing confirmation, separately
  selectable worksheets/named tables, and frozen dataset versions;
- read-only, explicitly allowlisted Odoo 19 schema capture;
- governed business keys and scope, dataset-centric scalar and relationship
  mapping, deterministic semantic validation, immutable revisions, and
  exact-hash submissions.

Implemented after this draft but not yet included in this approval:

- browser constants, fallbacks, explicit Odoo-default intent, bounded previews,
  and allowlisted scalar transformations.

Not yet implemented or approved:

- governed lookup translations, mapping import/export, functional review, and
  approval;
- signed installer and reproducible packaged release;
- pinned disposable Odoo/PostgreSQL Compose laboratory;
- production target selection;
- Odoo create, update, delete, import, or reconciliation execution.

## 4. Application security controls

### Local browser boundary

- exact generated `Host` allowlist;
- rejection of `Forwarded` and `X-Forwarded-*` headers;
- cryptographically random, single-use launch token;
- token exchange followed by immediate redirect to remove it from the active
  page URL;
- signed session cookie containing no customer data or credentials:
  `HttpOnly`, `SameSite=Strict`, 30-minute expiry;
- CSRF token on state-changing forms;
- exact same-origin `Origin`/`Referer` validation and rejection of cross-site
  Fetch Metadata;
- no state changes through `GET`;
- no CORS middleware;
- restrictive CSP, framing denial, MIME sniffing prevention, permissions
  policy, same-origin opener/referrer policy, and `Cache-Control: no-store`;
- API documentation endpoints and Uvicorn access logs disabled;
- server concurrency and keep-alive bounds.

The loopback site currently uses HTTP, so its session cookie cannot use the
`Secure` attribute. The exposure is constrained to literal loopback, an
ephemeral port, exact-host validation, and a single-use launch secret. If
corporate policy mandates `Secure` cookies on localhost, the architecture must
add locally trusted TLS or revisit the native-wrapper decision.

### File ingestion

- `.csv` and `.xlsx` allowlist; legacy, macro-enabled, encrypted, arbitrary ZIP,
  symlink, device, network-path, and URL inputs rejected;
- extension, signature, and Office-container inspection;
- configured byte, archive-entry, expanded-size, row, column, and string
  limits;
- unsafe ZIP paths, suspicious compression ratios, XML bombs, macros, formulas,
  external links/connections, and embedded objects rejected;
- generated storage identifiers, partial-file staging, atomic rename, and
  SHA-256 evidence;
- isolated worker termination on timeout, failure, or memory-limit breach.

Endpoint antivirus or content-disarm integration remains an infrastructure
policy decision before real customer data is accepted.

### DuckDB

- embedded library only; no SQL console or mapping-supplied SQL;
- parameterized values and application-owned repositories;
- external access, extension autoinstall/autoload, and community extensions
  disabled;
- configuration locked after connection;
- 256 MiB connection memory limit and two-thread limit;
- separate registry metadata and per-project databases;
- project IDs validated as UUIDs and resolved below the configured project
  root.

## 5. Odoo security boundary

The current connector is read-only by capability:

- only Odoo 19 JSON-2 `fields_get` and `search_read` are dispatched;
- no `create`, `write`, `unlink`, import, server action, generic method call,
  `execute_kw`, or SQL interface exists;
- only DEV and TEST environments are accepted;
- remote targets require HTTPS and cross-host redirects are refused;
- local HTTP requires explicit local mode and literal `127.0.0.1` or `::1`;
- API keys are redacted from exceptions and object representations;
- target reads are projected, bounded by the migration plan, paginated, and
  deterministically ordered.

Defense in depth must be enforced in Odoo through a dedicated service user,
model ACLs, record rules, permitted-company context, field/model allowlists,
expiring API keys, and rotation/revocation procedures. Application-level
method omission does not replace Odoo-side authorization.

Local mapping and staging corrections may be audited and revalidated, but they
do not modify Odoo. Any future write capability must use a separate connector,
process, credential, frozen approved plan, allowlists, idempotency journal, and
security approval.

## 6. Infrastructure requirements

The pilot workstation must provide:

- supported and patched Windows under corporate endpoint management;
- BitLocker or equivalent full-disk encryption with governed recovery keys;
- EDR/antimalware and host firewall;
- a named, least-privilege user account; local administrator use is not the
  normal operating mode;
- approved Python runtime and dependency installation source;
- a supported browser;
- project storage outside Git, email, shared folders, and consumer or
  unapproved enterprise sync locations;
- backup behavior explicitly agreed for customer data;
- VPN or approved internal path and trusted TLS chain for remote Odoo.

The normal storage root is `%LOCALAPPDATA%\Impodo\projects`. The application
currently relies on inherited Windows filesystem permissions; explicit
creation and verification of an owner-only ACL is not yet implemented and is
a release gate for real customer data.

Odoo infrastructure must provide separate DEV and TEST endpoints before
production consideration. Odoo 20.0 requires a new API-compatibility,
authentication, module, ACL, and regression review.

## 7. Data protection and retention

Proposed default pending customer policy approval:

- classification recorded per project; default `CONFIDENTIAL`;
- customer exports, DuckDB data, snapshots, reports, and audit artifacts remain
  in the encrypted local project directory;
- support access defaults to disabled and requires explicit authorization;
- raw source files remain immutable; derived corrections retain provenance and
  approval evidence;
- credentials remain outside project artifacts;
- retain through reconciliation and acceptance, then 90 days;
- extensions require a documented customer decision;
- at expiry, remove project files, exported copies, and credential-store
  entries and record non-sensitive deletion evidence.

Application-level project encryption is not implemented. Confidentiality at
rest currently depends on BitLocker and Windows access controls. SSD deletion
assurance depends on full-disk encryption and key governance rather than file
overwriting.

## 8. Verification evidence

The latest default local run on 29 July 2026 executed 89 automated tests:

- 88 passed;
- one optional generated-review-workbook integration test was skipped because
  its Node.js/artifact-tool runtime is not installed in this workspace;
- no default-suite test failed.

Passing tests cover the loopback session boundary, Host/origin/CSRF controls,
security headers, local/remote URL separation, credential rebinding, DuckDB
security settings and schema migration, isolated file validation, realistic
browser CSV/XLSX named-table inspection, source confirmation, frozen selection,
allowlisted schema capture, mapping invalidation/versioning, read-connector
closure, governed business keys, relationship semantics, mapping compilation,
exact-hash validation/submission, API-key redaction, target pagination,
portable-ID rejection, deterministic manifests, and fail-closed migration
classifications.

The optional generated-review-workbook test was explicitly invoked and stopped
at its declared prerequisite because Node.js is unavailable. This does not
affect the Python/openpyxl browser XLSX acceptance evidence, but the report
toolchain remains an environment gate.

This is useful engineering evidence, not a penetration test or live
infrastructure acceptance.

## 9. Conditions before broader approval

Required before a sanitized local pilot:

1. Provide and verify the pinned Node.js/artifact-tool report runtime.
2. Produce a locked dependency set, SBOM, vulnerability scan, and secret scan.
3. Confirm managed-workstation, BitLocker, EDR, browser, and Python baselines.
4. Decide whether loopback HTTP without a `Secure` cookie meets corporate
   policy.

Required before real customer data or on-premise DEV/TEST access:

1. Enforce and test owner-only project-directory ACLs.
2. Approve classification, retention, backup, deletion, antivirus/CDR, and
   authorised-support policies.
3. Complete threat modeling and security review of the implemented release.
4. Run live DEV and TEST tests with Odoo-side ACL/record-rule evidence,
   approved TLS/VPN routing, API-key rotation/revocation, and unchanged sentinel
   `write_date` evidence.
5. Harden and schema-validate saved snapshot envelopes.
6. Complete the reviewed 100–300-record sanitized acceptance slice and
   expected-scale memory/resource tests.
7. Sign the installer/package and verify release-artifact integrity.

A future production write executor requires a separate architecture and
security approval.

## 10. Review decision

Proposed decision: **conditional approval for continued implementation and a
sanitized local pilot**, subject to section 9.

| Decision | Reviewer entry |
| --- | --- |
| Approved / approved with conditions / rejected |  |
| Conditions or required changes |  |
| Cybersecurity and infrastructure reviewer |  |
| Review date |  |
| Re-review trigger | Material architecture change, real customer data, on-premise access, Odoo 20.0, or any write capability |

## Detailed evidence

- [Local application and security architecture](local-application-security.md)
- [Migration project contract](contracts/migration-project.md)
- [Read connector contract](contracts/read-connector.md)
- [Acceptance and test strategy](testing/acceptance.md)
- [Local browser operating model](operations/local-browser.md)
- [Python dependencies](../pyproject.toml)
