---
audience: developer
kind: contract
status: current
---

# Project lifecycle contract

## Scope

One migration project is the governance boundary for one migration scope. Its
setup selects exactly one source mode: governed files (`FILE`) or existing
records in the configured Odoo database (`ODOO`). A project is not created per
Odoo model.

The browser is the normal authoring interface. Stored manifests, hashes, audit
events, and DuckDB records are machine evidence and must not be edited directly.

## Lifecycle

The implemented transition is:

```text
DRAFT --register--> REGISTERED
```

`CLOSED` is reserved in the domain model but has no browser transition.

Registration requires project ownership, governance, source mode, and exact
target configuration. `FILE` mode additionally requires an export date and at
least one governed CSV or XLSX file. `ODOO` mode rejects file attachment and
does not require an export date.

Registration freezes the business and target setup, increments the optimistic
revision, publishes canonical registration evidence, and records an actor-bound
audit event. Later stages create their own versioned evidence; they never reopen
the registered aggregate.

## Source boundary

Draft file content is size-bounded, validated in an isolated worker, stored
under generated identifiers, and SHA-256 hashed. It is never edited in place.

A registered file project may add or remove an incorrect source file only
before the first source-table selection is frozen. Removal is revision checked,
deletes only that file's catalogue, confirmation, and contained bytes, and
records an audit event. After source freeze, file changes fail closed.

An Odoo-source registration performs no business-record read. Bounded model,
field, and record selection remains a separately authorized workflow.

## Target and credential boundary

Target identity binds connection mode, normalized endpoint, and database:

- `LOCAL` permits HTTP only on literal loopback targets;
- `REMOTE` requires HTTPS and rejects loopback targets.

Changing mode, endpoint, or database changes the target identity and
invalidates target-derived evidence.

An Odoo API key is not project data. Read and write credentials use separate
role-specific fields and vault entries and never fall back to one another.
Changing the target removes both roles for the old target; deleting the project
removes both roles. Stored evidence may retain only non-secret generation,
principal, permission, and context bindings.

Registration and connection configuration do not grant Odoo read or write
capability. Each connector operation requires its own narrow capability and
must revalidate the current target and credential binding.

## Authority and concurrency

Human-entered owner names are governance metadata, not authorization. Every
state-changing command receives a verified actor and capability. Optimistic
revision checks reject stale browser forms rather than overwriting newer state.

Audit events retain stable actor issuer and subject identities. Derived status
summaries are not approvals; an approval must bind an actor to exact immutable
evidence.

## Persistence boundary

The local composition stores a registry plus one protected project directory
and DuckDB database per project. Domain and application code access artifacts
through ports; filesystem paths are not domain contract values. Hosted
deployments must supply their own identity, database, storage, secret, and job
adapters.

Projects outside the exact supported database contract are not opened or
silently upgraded. Deletion resolves and removes only the exact registered,
contained project after authorization and credential cleanup.

## Related documentation

- [Project setup implementation](../workflow/00-project-setup.md)
- [Source data implementation](../workflow/01-source-data.md)
- [Security and infrastructure](../../architecture/security-and-infrastructure.md)
