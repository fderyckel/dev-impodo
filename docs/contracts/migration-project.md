# Migration project contract

## Status and purpose

**Status:** Integrated in the local browser.

One migration project is the governance boundary for one migration scope. It
may contain multiple source files, datasets, and Odoo models. A project is not
created per Odoo model.

The browser is the normal authoring interface. Canonical manifests and DuckDB
records are machine evidence; users do not edit them directly.

## Lifecycle

The implemented transition is:

```text
DRAFT --register--> REGISTERED
```

Draft project metadata and source files are editable through revision-checked
commands. Registration requires:

- project name and source system;
- a received source export and export date;
- at least one governed CSV/XLSX source file;
- responsible data manager and functional owner;
- data classification and retention policy;
- `LOCAL` or `REMOTE` Odoo mode, exact base URL, and database.

Registration freezes the source-file evidence, increments the optimistic
revision, writes canonical registration evidence, and records an actor-bound
audit event. Source discovery, schema capture, and mapping then create their
own versioned artifacts without reopening the registered project.

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

Source files are stored under generated identifiers, size-bounded, validated
in an isolated worker, SHA-256 hashed, and immutable after registration.
Worksheet/table selection and dataset freezing belong to the
[workspace contract](workspace.md).

Target security is controlled by connection mode, not by organizational
lifecycle labels:

- `LOCAL` permits HTTP only for literal loopback targets;
- `REMOTE` requires HTTPS and rejects loopback targets.

Changing the target mode, URL, or database changes the target identity and
invalidates target-derived evidence.

An Odoo API key is not a project field. Remote credentials remain in memory
or the operating-system credential vault and are bound to the project, mode,
exact URL, and database. Local no-key discovery keeps selected machine paths
in process memory only.

## Persistence boundary

The local adapter uses a small project registry plus one protected directory
and DuckDB database per project. Application services access files through an
artifact-store port; filesystem paths are not part of the domain contract.
A hosted adapter must supply its own identity, database, storage, secret, and
job implementations.

Project registration grants no Odoo write capability. Browser and storage
controls are documented in
[security and infrastructure](../architecture/security-and-infrastructure.md).
