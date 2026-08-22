---
audience: developer
kind: contract
status: current
---

# Contained project lifecycle contract

## Scope

One `MigrationProject` provides the contained workspace for one DataVersion in
a Recipe. It stores that DataVersion's evidence, credentials, authorization
state, and files. Setup selects exactly one source mode: governed files
(`FILE`) or existing records in one configured Odoo database (`ODOO`). Impodo
does not create a separate workspace for each Odoo model. The browser label
**project** refers to the owning Recipe; this contract uses **workspace** when
it means `MigrationProject`.

The browser is the normal authoring interface. Stored manifests, hashes, audit
events, and DuckDB records are machine evidence and must not be edited directly.

## Lifecycle

The implemented transition is:

```text
DRAFT --register--> REGISTERED
```

`CLOSED` is reserved in the domain model but has no browser transition.

Registration requires the workspace name, stable source-system label, and one
exact source mode. `FILE` mode also requires at least one governed CSV or XLSX
file. It does not require an export date, ownership fields, governance fields,
or an Odoo destination. `ODOO` mode rejects file attachments and requires the
source connection mode, normalized endpoint, and database. Before registering
an Odoo-source workspace, the normal browser also requires a successful
purpose-specific read check.

Registration fixes the source mode, increments the optimistic revision,
publishes canonical registration evidence, and records an actor-bound audit
event. Later stages create their own versioned evidence. A registered file
workspace may still receive or replace its Odoo destination in the Odoo-data
stage; that governed change invalidates target-derived evidence rather than
reopening source setup.

## Source boundary

Draft file content is size-bounded, validated in an isolated worker, stored
under generated identifiers, and SHA-256 hashed. It is never edited in place.

A registered file workspace may add or remove an incorrect source file only
before the first source-table selection is frozen. Removal is revision checked,
deletes only that file's catalogue, confirmation, and contained bytes, and
records an audit event. After source freeze, file changes fail closed.

An Odoo-source registration performs no business-record read. Bounded model,
field, and record selection remains a separately authorized workflow.

## Target and credential boundary

Target identity binds connection mode, normalized endpoint, and database. For
file sources it is configured after source freeze in the Odoo-data stage. For
Odoo sources the same identity is established during initial source setup and
remains both capture origin and pinned-comparison target:

- `LOCAL` permits HTTP only when the target is a literal loopback address.
- `REMOTE` requires HTTPS and rejects loopback addresses.

Changing mode, endpoint, or database changes the target identity and
invalidates target-derived schema, mapping, comparison, and execution evidence.
It never changes reusable Recipe meaning by itself.

An Odoo API key is neither Recipe meaning nor workspace data. Read and write
credentials use separate role-specific fields and vault entries and never fall
back to one another.
Changing the target removes both roles for the old target; deleting the owning
unpublished Recipe draft removes both roles for its contained workspace.
Stored evidence may retain only non-secret generation, principal, permission,
and context bindings.

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

The local composition stores Recipe and DataVersion lineage in the registry.
It uses one application-encrypted protected Recipe store and creates one
protected directory with a DuckDB database for each DataVersion workspace.
Domain and application code access artifacts through ports; filesystem paths
are not domain contract values. Hosted deployments must supply their own
identity, database, storage, secret, and job adapters.

Every current workspace registry row has one exact Recipe/DataVersion linkage
from its creation operation. The current build does not backfill or lazily
adopt an unlinked standalone workspace. Sealed DataVersion workspaces reject
mutation. Direct workspace deletion is unsupported; deleting an unpublished
Recipe draft validates its exact Recipe and workspace revisions before it
removes the contained workspace.

Workspace databases outside the exact supported base contract are not opened.
Checksum-pinned additive Recipe linkage migrations are the only current
in-place schema migration mechanism.

## Related documentation

- [Recipe and data-version lifecycle](recipe-lifecycle.md)
- [Recipe and data-version setup](../workflow/00-project-setup.md)
- [Source data implementation](../workflow/01-source-data.md)
- [Security and infrastructure](../../architecture/security-and-infrastructure.md)
