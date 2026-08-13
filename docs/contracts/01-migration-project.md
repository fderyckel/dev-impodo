# Migration project contract

## Status and purpose

**Status:** Integrated in the local browser.

One migration project is the governance boundary for one migration scope. Its
immutable setup chooses one source mode: governed files (`FILE`) or existing
records in the configured Odoo database (`ODOO`). A project is not created per
Odoo model.

The browser is the normal authoring interface. Canonical manifests and DuckDB
records are machine evidence; users do not edit them directly.

## Lifecycle

The implemented transition is:

```text
DRAFT --register--> REGISTERED
```

Draft project metadata and, for `FILE` mode, source files are editable through
revision-checked commands. A registered `FILE` project may add a replacement or
remove an incorrect source file only until its first table selection is frozen. The removal retires
that file's catalogue and confirmation, deletes its contained stored bytes, and
records an actor-bound audit event; it never edits the source bytes in place.
Registration always requires:

- project name and source system;
- responsible data manager and functional owner;
- data classification and retention policy;
- `LOCAL` or `REMOTE` Odoo mode, exact base URL, and database.

`FILE` registration additionally requires a received source export, its export
date, and at least one governed CSV/XLSX file. `ODOO` registration requires no
export date or placeholder file and rejects file attachment. Its registered
next step is read-only Odoo model discovery and capture-eligibility metadata;
bounded record selection/freezing is not implemented by this slice yet.

Registration freezes the business and target setup, increments the optimistic
revision, writes version-4 canonical registration evidence, and records an
actor-bound audit event. The source-file list remains amendable only through the
governed early-stage add/remove commands until table selection freezes the source
boundary. Existing schema-version-1 project databases migrate
forward as `FILE` without changing their source evidence. Source discovery,
schema capture, and mapping then create their own versioned artifacts without
reopening the registered project.

`CLOSED` is reserved in the domain model but has no implemented browser
transition yet.

## Authority and audit

Human-entered owner names are governance metadata, not authorization. Every
state-changing command receives a verified actor and capability. Optimistic
revisions reject stale browser forms rather than overwriting newer changes.

Audit events retain stable actor issuer/subject identities. Derived status
summaries are never authoritative approval; an approval must bind an actor to
exact immutable evidence.

## Source and target evidence

In `FILE` mode, source files are stored under generated identifiers,
size-bounded, validated in an isolated worker, SHA-256 hashed, and never edited
in place. An incorrect file may be removed before dataset freezing; after that
boundary the registered source evidence is immutable. Worksheet/table selection and dataset freezing belong to
the [workspace contract](02-workspace.md). In `ODOO` mode, registration itself
does not read business records and grants no read or write capability beyond
the separately authorized target operations.

Target security is controlled by connection mode, not by organizational
lifecycle labels:

- `LOCAL` permits HTTP only for literal loopback targets;
- `REMOTE` requires HTTPS and rejects loopback targets.

Changing the target mode, URL, or database changes the target identity and
invalidates target-derived evidence.

That identity is explicitly the `connection_target_hash`: mode, normalized
endpoint, and database name. It is not a strong database-instance identity.
The current Odoo-source policy records connection-only assurance and disables
native production writes because restore/clone identity and atomic
compare-and-write are unavailable through the closed JSON-2 surface.

An Odoo API key is not a project field. Read and write credentials occupy
separate role- and target-specific entries in memory or the operating-system
credential vault, use separate browser fields and vault service labels, and
never fall back to one another. Changing the target deletes both roles for the
old target; deleting the project removes both roles. Each present entry
produces a non-secret registry removal receipt that survives project deletion.
Local no-key discovery keeps selected machine paths in process memory only.

Each stored role uses a versioned vault envelope with a random generation ID.
Model and schema catalogues bind the read credential generation through a
non-secret `read_credential_binding_hash`; re-entering or rotating a key creates
a new binding without persisting a secret-derived verifier.

For remote reads, a separate closed JSON-2 probe uses `res.users/context_get`,
one exact self-record read, a bounded active-company ID projection, and model-
level `has_access('read')` checks. It stores only hashes for the authenticated
principal, observed direct groups and model-read outcomes, and effective
language/timezone/company/active-record context; raw user, group, and company
identifiers are not persisted. The observed permission hash is not a complete
ACL or record-rule configuration digest. Local no-key shell metadata therefore
keeps these identity hashes empty rather than claiming principal parity it
cannot prove.

Remote execution uses a different closed probe and the write-role credential.
It requires model-level read access for every model in the immutable reviewed
API scope and write access for every model with reviewed write fields. Before
any target I/O, the execution journal records only the random write-credential
generation hash plus write-principal, observed-permission, and context hashes.
Read-back re-probes the credential and rejects a changed target, principal,
permission scope, or context. These checks remain model-level observations;
the later guarded-update phase must still prove access to each exact record and
baseline field.

Successful read/write credential storage and replacement append actor-bound
project audit events. Their details contain only the non-secret binding hash
and whether storage is session-only or in the operating-system vault; API keys
and raw Odoo user/group/company identifiers are excluded.

## Persistence boundary

The local adapter uses a small project registry plus one protected directory
and DuckDB database per project. Application services access files through an
artifact-store port; filesystem paths are not part of the domain contract.
A hosted adapter must supply its own identity, database, storage, secret, and
job implementations.

A project outside the exact current database contract is not opened or
upgraded. The project list explains that it must be recreated, while the normal
revision-checked deletion command remains available. Deletion reads only the
registry identity needed to authorize, clean up, and remove that exact
contained project.

Project registration grants no Odoo write capability. Browser and storage
controls are documented in
[security and infrastructure](../architecture/security-and-infrastructure.md).
