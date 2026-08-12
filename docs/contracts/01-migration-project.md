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
revision-checked commands. Registration always requires:

- project name and source system;
- responsible data manager and functional owner;
- data classification and retention policy;
- `LOCAL` or `REMOTE` Odoo mode, exact base URL, and database.

`FILE` registration additionally requires a received source export, its export
date, and at least one governed CSV/XLSX file. `ODOO` registration requires no
export date or placeholder file and rejects file attachment. Its registered
next step is read-only Odoo model discovery and capture-eligibility metadata;
bounded record selection/freezing is not implemented by this slice yet.

Registration freezes the selected source-mode setup, increments the optimistic
revision, writes version-4 canonical registration evidence, and records an
actor-bound audit event. Existing schema-version-1 project databases migrate
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
size-bounded, validated in an isolated worker, SHA-256 hashed, and immutable
after registration. Worksheet/table selection and dataset freezing belong to
the [workspace contract](02-workspace.md). In `ODOO` mode, registration itself
does not read business records and grants no read or write capability beyond
the separately authorized target operations.

Target security is controlled by connection mode, not by organizational
lifecycle labels:

- `LOCAL` permits HTTP only for literal loopback targets;
- `REMOTE` requires HTTPS and rejects loopback targets.

Changing the target mode, URL, or database changes the target identity and
invalidates target-derived evidence.

An Odoo API key is not a project field. Read and write credentials occupy
separate role- and target-specific entries in memory or the operating-system
credential vault, use separate browser fields and vault service labels, and
never fall back to one another. Changing the target deletes both roles for the
old target; deleting the project removes both roles and the retired shared
entry. Local no-key discovery keeps selected machine paths in process memory
only.

Each stored role uses a versioned vault envelope with a random generation ID.
Model and schema catalogues bind the read credential generation through a
non-secret `read_credential_binding_hash`; re-entering or rotating a key creates
a new binding without persisting a secret-derived verifier. This is rotation
evidence, not proof of the authenticated Odoo principal or its current
permissions. A narrow principal/permission probe remains a separate planned
contract.

## Persistence boundary

The local adapter uses a small project registry plus one protected directory
and DuckDB database per project. Application services access files through an
artifact-store port; filesystem paths are not part of the domain contract.
A hosted adapter must supply its own identity, database, storage, secret, and
job implementations.

A project outside the supported database baseline is not opened or upgraded
implicitly. The project list explains that it must be recreated, while the
normal revision-checked deletion command remains available. Deletion reads only
the legacy project identity needed to authorize, clean up, and remove that exact
contained project.

Project registration grants no Odoo write capability. Browser and storage
controls are documented in
[security and infrastructure](../architecture/security-and-infrastructure.md).
