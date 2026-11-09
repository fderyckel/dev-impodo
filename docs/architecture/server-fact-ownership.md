---
audience: developer
kind: architecture
status: current
---

# Shared-server fact ownership

## Running application

The Linux container rehearsal serves health checks, sign-in, account recovery,
and account administration. It uses PostgreSQL and serves HTTPS through a
loopback proxy. The shared migration application refuses startup, and the
rehearsal runs without a migration worker. The full migration workflow runs in
the local application. See the [rehearsal runbook](../../deploy/README.md) and
[server composition](../../src/impodo/web/composition/server_app.py).

The PostgreSQL Project catalogue, operation journal, and Workspace leases are
implemented repositories. The scenario Preparation adapter exercises them
with DuckDB and Parquet in tests. The rehearsal browser routes use the
identity repositories. See
[Linux web composition](../../src/impodo/web/composition/server_linux_web.py)
and the [cross-store Preparation test](../../tests/platform/linux/test_preparation_cross_store.py).

## Store boundaries

An authoritative store records what happened. A projection summarizes that
record for lookup and can be rebuilt from its receipt. A storage key locates
an artifact, while its content hash identifies the exact bytes.

| Store | What it owns |
| --- | --- |
| PostgreSQL control schema | Organizations, accounts, login identities, password verifiers, throttles, one-time token and session digests, and security events. The schema also holds the Organization-scoped Project catalogue, operation reservations, Workspace leases, receipts, and current projection pointers. |
| Local DuckDB stores | The local registry records Project discovery and the links between Data versions, Recipes, runs, and Workspaces. The contained stores hold detailed source choices, mappings, preparation, approvals, execution journals, and reconciliation. |
| Artifacts | Original uploads, immutable Parquet snapshots, Recipe payloads, and reports hold the files and bulk evidence. Protected artifacts hold restricted Odoo identifiers and target evidence. |
| Secret storage | The local credential vault holds Odoo keys and encryption keys. The Linux trial reads its database credentials and identifier-digest key from protected mounted files. |

The [PostgreSQL schema](../../src/impodo/adapters/postgresql/migrations.py) is
version `1`. Its runtime role can append receipts and update specific control
fields. Receipt rows describe the operation, its scope, and evidence hashes
with bounded artifact metadata. Migration values, mappings, approvals, and
Odoo outcomes remain in the migration stores. See
[ADR-017](../decisions/README.md#adr-017--organization-scoped-postgresql-control-and-duckdb-data-planes)
for the design decision.

## Account scope

- The first-operator command creates the Platform Administrator, login
  identity, password verifier, and security event in one transaction.
- The Platform Administrator manages Organizations and assigns their
  administrators. The active Organization Administrator manages ordinary
  users, their sessions, and security history in that Organization.
- A customer login belongs to exactly one Organization. Session resolution
  checks the current account, Organization state, security revision, and
  administrator assignment where applicable. Account administration grants
  account access only.
- Login reads the verifier and throttles in a short transaction. Password
  verification and rehashing run outside it. A separate transaction records
  the outcome and applies the throttle or verifier updates.
- Activation consumes the token and sets the first password in one
  transaction. Password reset consumes the token, changes the password,
  revokes sibling tokens and sessions, and records the event atomically.

The [identity administration repository](../../src/impodo/adapters/postgresql/identity_administration.py)
applies the Organization filter before any optional user filter when paging
security history and sessions. The browser runs synchronous account work off
the event loop and opens no DuckDB store for these requests.

## Project catalogue and leases

The [Project catalogue repository](../../src/impodo/adapters/postgresql/project_catalogue.py)
scopes lookup and updates by Organization. Discovery applies the supplied
Organization-wide visibility or list of authorized Project IDs before paging.
A ready catalogue entry requires an artifact root key and a receipt hash.
The catalogue records lifecycle and deletion state as metadata.

The [operation journal and lease repositories](../../src/impodo/adapters/postgresql/coordination.py)
use short PostgreSQL transactions. A reservation binds an Organization's
idempotency key to the operation's exact meaning. A lease binds one Workspace
to an attempt and a fencing token. A new acquisition increases the token, and
heartbeat and release require the current lease identity and version.

## Cross-store Preparation

The [coordinator](../../src/impodo/application/shared/cross_store_coordinator.py)
accepts a request with an Organization, Project, operation, and authorization
receipt ID. The [scenario adapter](../../src/impodo/adapters/duckdb/preparation_operation.py)
provides one concrete data-plane command for a verified file scenario.

1. The coordinator reserves the request and acquires the Workspace lease.
2. The adapter stages a Parquet snapshot. It checks lease expiry and commits
   the receipt with the Workspace fence in DuckDB, then publishes the exact
   artifact bytes.
3. The coordinator records the data-plane receipt and verifies the artifact
   key, size, and hash. It records the matching projection receipt in
   PostgreSQL. A stale receipt cannot replace a newer current pointer.
4. The coordinator releases the lease after success.

Each store commits independently. The receipts identify what completed if a
process stops between commits.

| Evidence on retry | Recovery |
| --- | --- |
| The reservation has no committed data-plane receipt. | The coordinator checks the Workspace for a receipt. If it finds none, it records an interruption. |
| DuckDB committed a receipt and the published bytes verify. | The coordinator replays the receipt and publishes the matching projection. |
| DuckDB committed a receipt but the artifact is missing or has different bytes. | The coordinator records manual review and withholds the projection. |
| The projection already committed. | The retry returns the saved result. |
| The idempotency key was reused with different input. | The journal rejects the reservation. |

[Projection reconciliation](../../src/impodo/application/shared/projection_reconciliation.py)
requires Project audit access for a bounded, hash-verified scan in PostgreSQL.
Rebuilding requires Project administration and a proven committed data-plane
receipt with a missing projection. It reuses the receipt without rerunning
Preparation.

## Local application

The local launcher trusts one operator. Its migration registry, file stores,
credential vault, and process-local job managers run independently of
PostgreSQL and server account administration. See
[security and infrastructure](security-and-infrastructure.md) for the browser,
storage, and Odoo access controls.
