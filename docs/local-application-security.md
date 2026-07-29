# Local application and security architecture

## 1. Decision

**Accepted initial deployment:** a local-only browser application.

Impodo runs as a Python process on the data manager's machine, listens only on
the IPv4 loopback address, chooses an ephemeral port, and opens the user's
default browser. It is not reachable from the LAN and does not upload customer
data to a cloud service.

This gives the data manager a guided UI without introducing a hosted
multi-user platform before identity, tenancy, infrastructure, and retention
requirements are known.

**Implementation status:** the local browser now implements the secured
loopback launch/session boundary, governed project forms, a resource-bounded
source-validation worker, per-project DuckDB storage, Windows credential-store
integration, registration evidence, and explicit local-versus-remote Odoo
connection modes. Its first Phase B slice also provides resource-bounded,
hash-bound CSV/XLSX inventory, preview, and column profiles. The CLI continues
to implement strict CSV/XLSX preparation and read-only Odoo preflight.
Interactive source-setting confirmation, mapping, signed packaging, and a
packaged Odoo Compose laboratory remain later delivery slices.

## 2. Proposed stack

| Concern | Initial choice | Reason |
| --- | --- | --- |
| Application backend | Python with FastAPI/Starlette | Reuses the typed Python domain and provides a small, testable HTTP boundary |
| Local server | Uvicorn on `127.0.0.1` and an ephemeral port | Keeps the service off LAN interfaces and avoids a fixed discoverable port |
| UI | Server-rendered Jinja templates plus HTMX and minimal vanilla JavaScript | Supports a guided mapping UI without a large client runtime |
| Static assets | Vendored with Impodo; no CDN | Prevents runtime asset calls and third-party browser tracking |
| CSV | Python standard-library `csv` parser | Mature parser with no additional runtime dependency |
| XLSX | `openpyxl` read-only plus `defusedxml` | Streams worksheet cells and fails closed without XML-bomb protection |
| Bulk staging | Embedded DuckDB behind internal repositories | Handles typed tabular data locally without a separate database service |
| Mapping/project metadata | Governed tables in the same local project store initially | Binds mappings, evidence, and staged rows to one project |
| Secrets | Operating-system credential store; memory-only use during a run | Avoids project files, browser storage, logs, and source control |
| Local Odoo laboratory | Loopback-bound Odoo 19 checkout initially; pinned Compose stack later | Enables immediate read-only testing without weakening remote HTTPS rules |
| Packaging | Signed local launcher/installer after the workflow stabilizes | Gives the data manager a verifiable desktop entry point |

The UI does not need React, Electron, a public web server, or a Node.js runtime.
Keeping server-side rendering and browser code small reduces dependencies and
the client-side attack surface.

A Tauri/native shell can be evaluated later if enterprise desktop packaging,
managed updates, or stronger WebView isolation justify the additional Rust and
code-signing lifecycle. The native-wrapper option is deferred for the first
release. Electron is not recommended for the first release.

## 3. Security goals

The local application must protect against:

- accidental LAN or internet exposure;
- malicious websites attempting to call a localhost service;
- CSRF, DNS rebinding, clickjacking, and untrusted browser origins;
- malicious or malformed spreadsheets;
- ZIP/XML decompression bombs in `.xlsx`;
- path traversal and overwriting local files;
- formula content becoming executable in review workbooks;
- CPU, memory, or disk exhaustion;
- customer-data leakage through logs, telemetry, temp files, or Git;
- Odoo credential leakage;
- accidental use of production or a write-capable account;
- arbitrary SQL, Python, shell, or Odoo method execution.

Local-only does not protect against malware already running as the same
operating-system user. Full-disk encryption, endpoint protection, patching, and
least-privilege OS accounts remain required.

## 4. Loopback web controls

The application must:

1. bind only to `127.0.0.1`, never `0.0.0.0`;
2. let the operating system select an unused ephemeral port;
3. accept only the exact generated `Host` value;
4. reject proxy and forwarded-host headers because no proxy is expected;
5. configure no CORS origins;
6. use a cryptographically random per-launch session token;
7. transfer the launch token into an `HttpOnly`, `SameSite=Strict` session and
   remove it from the visible URL;
8. require a synchronizer CSRF token on every state-changing request;
9. require an exact same-origin `Origin` or `Referer`, and reject cross-site
   Fetch Metadata when that optional browser header is present;
10. reject state changes through `GET`;
11. set a strict Content Security Policy using local assets only;
12. deny framing with `frame-ancestors 'none'`;
13. set `X-Content-Type-Options: nosniff`;
14. avoid browser `localStorage` for secrets or customer data;
15. expire sessions after inactivity and when the local process exits;
16. redact request bodies, cells, tokens, and secrets from logs.

The generated origin is the only trusted browser origin. Loopback binding is
necessary but not sufficient by itself.

## 5. File-ingestion controls

Only `.xlsx` and `.csv` are accepted initially.

Explicitly reject:

- `.xls`;
- `.xlsm`, `.xltm`, and macro-enabled Office files;
- password-protected or encrypted workbooks;
- arbitrary ZIP uploads;
- symlinks, device files, URLs, and network paths supplied as upload names.

For every accepted file:

- enforce configured compressed and uncompressed size limits;
- inspect extension, signature, and container structure rather than trusting
  browser `Content-Type`;
- replace the storage name with an application-generated ID;
- retain the original display name as escaped metadata only;
- copy the input into the governed project inbox;
- open files read-only;
- calculate a SHA-256 hash before parsing;
- cap workbook entry count, worksheet count, row count, column count, string
  length, shared-string count, and total expanded bytes;
- reject suspicious compression ratios and unsafe ZIP member paths;
- use XML bomb protections;
- run parsing in a separate worker process with time and memory limits;
- never execute macros, formulas, links, embedded objects, or external data
  connections;
- record formula cells as evidence and require an explicit cached-value or
  reject policy;
- delete worker temporary files after success or failure.

Production deployments should integrate approved endpoint antivirus or
content-disarm scanning when the organization's security policy requires it.

## 6. Staging-store controls

The initial local store is one DuckDB database per migration project under:

```text
var/projects/<project-id>/
├── inbox/
├── staging/
├── snapshots/
├── reports/
└── audit/
```

`var/` is inside the project during local development but is excluded from
Git. Customer data is never stored under `examples/`, `fixtures/`, or another
tracked path.

DuckDB is embedded through its Python library, not invoked through its CLI.
Impodo must:

- expose no SQL editor or mapping-supplied SQL;
- use parameterized values and internally controlled identifiers;
- disable extension autoinstall and autoload;
- disable community and unsigned extensions;
- disable external access after Impodo has opened its own database;
- set memory and thread limits;
- lock security configuration;
- prohibit user-supplied file paths in database functions;
- use only application-owned repository methods;
- close and checkpoint the database cleanly;
- hash exported staged packages.

If a capability needs Parquet, Impodo performs the operation through a reviewed
internal path, not a user-entered SQL statement.

Project directories use owner-only permissions. FileVault, BitLocker, LUKS, or
equivalent full-disk encryption is an environmental prerequisite for real
customer data. Application-level project encryption can be added when the
retention and key-management policy is known.

### Proposed first-release data handling policy

The data manager is the local custodian of a migration project. Until the
customer agrees a different written policy, Impodo SHOULD apply the following
default:

- customer exports, DuckDB staging data, target snapshots, review packages,
  and execution journals stay only in the encrypted local project directory;
- the directory is accessible only to the data manager's operating-system
  account (and a separately authorised support account where the customer
  permits one); it MUST NOT be placed in a shared drive, consumer-sync folder,
  email attachment, Git repository, or telemetry service;
- source files remain immutable after intake. Corrections and derived staged
  data are separate, versioned artifacts with before/after values, rule or
  reason, operator, and timestamp;
- credentials remain in the operating-system credential store and are deleted
  when the project closes; they never enter the DuckDB database or report;
- keep the complete project through reconciliation and acceptance, then retain
  it for 90 days by default for audit and restart purposes. A longer period
  requires the customer's documented retention decision;
- at expiry, the data manager deletes the project directory, locally exported
  copies, and related credential-store entries, and records the deletion in a
  non-sensitive project register. On SSDs, rely on full-disk encryption and
  access-key removal rather than claiming that file overwriting is reliable.

The data manager must confirm the customer's classification, retention period,
backup policy, and permitted support access before real customer data is
accepted. This proposal intentionally does not authorize cloud backup or
remote support access by default.

For a later hosted, multi-user deployment, PostgreSQL should replace the local
project store so users, roles, tenant isolation, concurrent runs, backups, and
audit retention can be governed centrally.

## 7. Secret handling

Odoo credentials must not be stored in:

- mappings;
- profiles;
- project databases;
- uploaded files;
- command histories;
- logs;
- reports;
- browser storage;
- Git or `.env` files.

The local UI obtains an API key from the operating-system credential store or
asks for it for the current session. The key is held in memory only while
needed and is redacted from exceptions.

Use separate credentials for:

1. read-only schema discovery and preflight;
2. future approved DEV/TEST writes;
3. future production writes.

A read credential must never be silently upgraded to write capability.

## 8. Local Odoo laboratory

The browser can use an existing Odoo 19 development checkout when it is bound
to a literal loopback address. Local HTTP is allowed only in this explicit
mode. PostgreSQL must not be exposed through Impodo, and the same API-key and
read-only connector boundary applies.

For a future reproducible and disposable laboratory, use Docker Compose with:

- an official Odoo 19 image pinned to an immutable digest;
- an official PostgreSQL image pinned to an immutable digest;
- a private Compose network;
- PostgreSQL available only to the Odoo container;
- Odoo published as `127.0.0.1:<chosen-port>:8069`;
- no SMTP, external storage, or unrelated integrations;
- randomized local credentials stored in ignored secret files;
- a disabled or tightly controlled database manager;
- sanitized test data only;
- disposable DEV databases;
- optional reviewed custom addons mounted read-only.

The local Community image proves Impodo's generic transport, schema,
preparation, mapping, and relation behavior. It does not prove compatibility
with the future on-premise database's Enterprise modules, custom modules,
record rules, or data.

No local test should require access to the eventual on-premise Odoo instance.

## 9. Eventual on-premise connection

Impodo should initiate outbound HTTPS connections to Odoo. No inbound
connection from Odoo to the data manager's workstation is required.

The on-premise deployment must provide:

- Odoo 19.4 for the initial delivery; the planned Odoo 20.0 move in September
  requires a compatibility check and a new DEV/TEST rehearsal before it is
  used for a migration;
- DEV and TEST endpoints before production;
- VPN or approved internal network path;
- TLS using a certificate trusted by the workstation;
- target hostname and database-routing rule;
- dedicated Odoo bot users;
- explicit ACLs, record rules, and field access;
- API keys with expiration and rotation;
- relevant company and language context;
- model and field allowlists;
- source and target retention classifications;
- audit contacts and incident-revocation procedure.

Impodo must not receive PostgreSQL credentials, SSH access, Odoo master
passwords, administrator cookies, or a generic server-action capability.

The current read-only connector is suitable for the initial Odoo 19.4 target
because it uses Odoo 19 JSON-2. Odoo 20.0 support is a planned compatibility
milestone, not an assumption; its API contract, authentication, metadata, and
write rehearsal must be verified against the deployed version. If a target is
earlier than Odoo 19, a separately reviewed read adapter is required.

## 10. Controlled local changes and Odoo writes

The mapping workspace may validate data and make governed changes to its local
draft mapping, correction proposals, and derived staging records. It MUST NOT
modify an accepted raw source file. Every accepted local correction MUST retain
the raw value, canonical value, rule or reason, operator, timestamp, and
mapping/rule version. A local change invalidates any affected validation or
preflight result and requires it to be regenerated.

The current `OdooReadConnector` and preflight engine remain read-only. This is
not a restriction on the future product; it is the boundary that prevents a
mapping screen or validation run from changing Odoo accidentally.

The local browser application may later expose an approved import action, but
it must delegate only to a separate executor package and process with:

- a different connector interface;
- a different credential;
- DEV/TEST-only default configuration;
- a frozen, signed input-plan requirement;
- no arbitrary model or method;
- explicit model and field allowlists;
- dependency-ordered batches;
- idempotency keys and an execution journal;
- operator confirmation for sensitive actions;
- a separate security review and release gate.

This allows the data manager to correct and validate local staging data while
keeping Odoo mutations explicit, approval-bound, and independently auditable.

## 11. Security verification

Before a local UI release:

- static dependency and vulnerability scan;
- secret scan;
- loopback-binding test;
- Host, Origin, CSRF, Fetch Metadata, and CORS tests;
- content-security-policy test;
- malicious upload corpus;
- ZIP/XML bomb and oversized-file tests;
- filename and path-traversal tests;
- formula and hyperlink tests;
- parser worker timeout and memory-limit tests;
- DuckDB external-access and extension-lock tests;
- log and report leakage scans;
- packaging signature and reproducible dependency lock;
- local Odoo read-only integration test.

Before on-premise access:

- threat model with the customer's security team;
- data-flow and firewall review;
- account and ACL evidence;
- API-key rotation test;
- TLS and internal-CA verification;
- DEV/TEST sentinel `write_date` comparison;
- access-revocation drill;
- incident and data-deletion procedure.

## 12. Primary references

- [Uvicorn settings](https://www.uvicorn.org/settings/) — loopback is the
  default host; `0.0.0.0` exposes the service on the local network.
- [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
  — extension allowlists, type validation, generated names, size limits,
  storage isolation, and decompression-bomb controls.
- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
  — synchronizer tokens, origin checks, Fetch Metadata, and SameSite
  defense-in-depth.
- [OWASP Content Security Policy Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html)
  — CSP delivery and restrictive policy guidance.
- [openpyxl optimized modes](https://openpyxl.readthedocs.io/en/stable/optimized.html)
  and [openpyxl package security note](https://pypi.org/project/openpyxl/) —
  read-only workbook processing and the need for `defusedxml`.
- [Python ZIP-file decompression warning](https://docs.python.org/3/library/zipfile.html#decompression-pitfalls)
  — archive resource-exhaustion risk.
- [DuckDB security overview](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)
  and [extension security](https://duckdb.org/docs/current/operations_manual/securing_duckdb/securing_extensions)
  — external access, extension, and configuration controls.
- [Odoo 19 External JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html)
  — bearer API keys, access controls, dedicated bot users, and key rotation.
- [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/)
  — host-interface binding behavior.
- [Official Odoo container image](https://hub.docker.com/_/odoo) — local
  laboratory image source.
