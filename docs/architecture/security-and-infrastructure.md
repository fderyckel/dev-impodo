---
audience: developer
kind: architecture
status: current
---

# Security and infrastructure

The local application runs the migration workflow for one workstation
operator. The Linux container rehearsal runs health and account administration
behind a loopback HTTPS proxy. The two applications have separate session
and storage boundaries. See [shared-server fact ownership](server-fact-ownership.md).

## Local browser

The launcher binds a port selected by the operating system on `127.0.0.1`.
It exchanges a single-use launch token for a signed browser session and
removes the token from the active URL. A supervised child restart keeps the
session signing key. Starting a new launcher replaces the key.

State-changing routes check CSRF and same-origin requests. Middleware checks
the exact host and applies framing denial and `Cache-Control: no-store`.
The launcher disables API documentation, proxy forwarding, and access logs.
The session uses loopback HTTP with an HttpOnly cookie. The workstation
account and launch session establish the local operator's identity. That
operator receives all local capabilities, which services check before
governed operations. See the
[launcher](../../src/impodo/web/launcher.py) and
[loopback middleware](../../src/impodo/web/security.py).

## Source files and local storage

- Intake validates bounded CSV and XLSX uploads using file content. Spawned
  workers inspect files with time and memory limits. Validation covers
  workbook structure, compressed size, macros, external connections,
  embedded content, and formulas.
- Impodo stages uploads under generated filenames, checks their SHA-256
  hashes, and publishes immutable originals. It freezes confirmed source
  tables as immutable Parquet snapshots. The Data version owns the accepted
  data, and each Workspace stores references to the datasets it uses.
- The Project registry, Data version stores, and Workspace stores have
  separate DuckDB owners. Standard repository connections disable external
  access and extension autoload. Specific snapshot adapters enable filesystem
  access for application-controlled paths.
- The Windows default is `%LOCALAPPDATA%\Impodo\projects`. Normal mode
  verifies owner-restricted filesystem permissions and rejects Git
  checkouts, sync roots, network drives, symlinks, and junctions. Development
  mode is for synthetic or disposable data.
- Application-constructed snapshot paths stay inside the owner root.
  Prepared Parquet is immutable. Its manifest binds the source, mapping,
  schema, transformation, row count, and file hash before the current
  pointer advances.

Impodo encrypts protected Recipe payloads, qualification evidence, and
restricted Odoo evidence with AES-256-GCM. The vault holds the scoped keys.
Bulk source files, workspace data, and ordinary reports rely on filesystem
permissions and workstation disk encryption. Post-load field-difference
artifacts use that same owner-restricted storage and bind the exact file by
hash. See the
[Windows workstation requirements](../developer/setup/windows.md),
[artifact store](../../src/impodo/adapters/artifacts/local_store.py), and
[project root controls](../../src/impodo/adapters/protected_evidence/project_security.py).

## Credentials and Odoo reads

Impodo supports Odoo 19 and final Odoo 20. Local reads use fixed scripts with
the selected `odoo.conf`. Optional stack controls can stop only the Odoo and
PostgreSQL processes the current Impodo session started. Remote reads require
HTTPS outside literal loopback. The JSON-2 read adapter uses bounded model
and record reads, selected default lookups, and fixed identity and
permission probes. It exposes no caller-selected API method. See the
[local reader](../../src/impodo/adapters/odoo/local_reader.py) and
[JSON-2 read connector](../../src/impodo/adapters/odoo/connectors.py).

API keys stay in process memory or the operating-system vault. Credential
bindings identify the owner, role, and exact target. Conventional file-source
loads use a write role for execution and read-back. Odoo-to-Odoo transfer uses
separate source-fetch and destination-transfer roles. Credentials never enter
migration evidence or portable Recipes. Impodo records non-secret credential
generations and identity hashes so it can detect changes and recheck current
access. See the
[credential vault](../../src/impodo/adapters/protected_evidence/credential_vault.py)
and [execution access contract](../developer/contracts/execution-and-reconciliation.md#authorization-and-binding).

The service account's ACLs, record rules, permitted companies, and field
permissions remain part of the Odoo security boundary. Impodo's model-level
access probes supplement those controls.

## Odoo writes and recovery

**Load into Odoo** requires explicit confirmation and a current reviewed
snapshot for the exact target. The service checks the target, credential role,
company context, and reviewed model and field scope before opening the writer.
The closed JSON-2 adapter permits exact-key reads, bounded create and External
ID import calls, and updates to exact reviewed records. Request bodies are
limited to 1 MiB. Odoo business actions, deletion, generic RPC, and SQL are
outside that surface. JSON-2 checks and writes use separate requests, so Odoo
can change between them.

Impodo journals each write attempt before sending it. If the outcome is
unknown, Impodo stops further writes and requires read-back before recovery.
For conventional loads, the selected continuation policy can isolate
classified rejections when the batch is known to have committed nothing.
Recovery and read-back remain bound to the original snapshot and journal. See
[execution and reconciliation](../developer/contracts/execution-and-reconciliation.md).

The qualified-plan Production path applies to file-source runs. It requires
an active Production run with a fresh frozen Data version, an authenticated
selected CutoverPlan, and a different same-major target from the qualified
Test run. It uses distinct Production read and write credentials. Impodo
repeats the authority and dependency checks before constructing a writer.
See the [Production run contract](../developer/contracts/production-run-lifecycle.md).

## Linux server rehearsal

The Compose rehearsal publishes only its loopback TLS proxy. The web process
runs with a fixed unprivileged identity and reads its secrets from protected
files. PostgreSQL has separate schema migration and runtime roles.

The server sets a Secure, host-only, HttpOnly session cookie and a separate
CSRF cookie. It checks the configured Host and Origin on account forms,
limits request bodies to 64 KiB, and denies framing and caching. The account
services use scrypt password verifiers, persistent throttles, and process-local
limits on concurrent login and token operations. PostgreSQL stores only
digests of session handles, CSRF secrets, and one-time tokens. Activation
and recovery links carry their tokens in URL fragments. The form script
removes the fragment from browser history and submits the token in a POST body.
Public recovery requests return a generic acknowledgement. Authorized
administrators deliver the links privately during the rehearsal. See the
[rehearsal runbook](../../deploy/README.md) and
[server browser middleware](../../src/impodo/web/composition/server_session_http.py).

## Data handling

Use the protected Project root for sources, stores, reports, and audit
evidence. Keep original files immutable and retain the evidence for reviewed
corrections. Odoo record IDs needed for recovery belong in protected evidence,
outside portable mappings and reports. The local default classification is
`INTERNAL`. Classification, retention, backup, legal holds, deletion, and
support access follow the data owner's policies.

The Windows runbook covers managed endpoints, disk encryption, antimalware,
host firewall, local storage, and approved bundles. Remote Odoo acceptance
covers TLS, network routing, and live account permissions. The platform
reference records the verification scope for Windows, macOS, and Linux. See
the [Windows workstation requirements](../developer/setup/windows.md),
[remote Odoo acceptance runbook](../developer/runbooks/remote-odoo-acceptance.md),
and [platform support and testing](../developer/reference/platform-support-and-testing.md).
