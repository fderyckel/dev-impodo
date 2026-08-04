# Migration project contract

## Purpose

A migration project is the Stage A governance boundary for every later source,
mapping, snapshot, preflight, approval, and reconciliation artifact. The local
browser is the normal authoring interface. The registered JSON manifest is the
portable machine contract; users do not edit it by hand.

## Lifecycle

```text
DRAFT --register--> REGISTERED --close--> CLOSED
```

Only a draft is editable in the first browser release. Registration increments
the optimistic revision, freezes the current source-file evidence, writes a
canonical manifest, and records an audit event.

The implemented browser does not yet revise or close a registered project.
Those transitions must be added before downstream workflow stages depend on them.

## User-entered fields

| Field | Draft | Required to register |
| --- | --- | --- |
| Project name | Required | Yes |
| Source system | Required | Yes |
| Export status | Defaults to `PLANNED` | Must be `RECEIVED` |
| Export date | Optional | Yes |
| Description | Optional | No |
| Responsible data manager | Optional | Yes |
| Functional owner | Optional | Yes |
| Business unit/legal entity | Optional | No |
| Data classification | Defaults to `CONFIDENTIAL` | Yes |
| Retention days | Defaults to `90` | Yes |
| Authorised support access | Defaults to false | Yes |
| Odoo connection mode | Optional | `LOCAL` or `REMOTE` |
| Odoo base URL | Optional | Literal loopback URL for `LOCAL`; HTTPS server URL for `REMOTE` |
| Odoo database | Optional | Yes |
| Intended applications | Optional | Browser discovery filter and reviewer context |
| Intended technical models | Optional during registration | Required and explicitly confirmed before Stage C field capture |

## System-controlled fields

- UUID project identifier;
- creation, update, and registration timestamps;
- optimistic revision;
- source storage names, byte sizes, SHA-256 hashes, and intake timestamps;
- project status;
- mapping version;
- current run;
- derived approval-summary status;
- audit-event identifiers, timestamps, and stable actor issuer/subject
  identities.

The human-entered data-manager and functional-owner names are governance
metadata, not authorization claims. State-changing application commands
receive a verified actor. The local deployment supplies one privileged local
operator; the future hosted adapter will resolve corporate identity,
capabilities, and project membership.

Approval-summary status is never the authoritative approval. Normalization
decisions and future export-plan approvals are immutable actor-bound records
attached to exact evidence hashes.

An Odoo API key is not a project field. Remote mode holds it in memory for the
current process or saves it separately in the operating-system credential
store. The credential identifier binds it to the project, connection mode,
exact URL, and database so it cannot be silently reused after a target change.
Local Windows mode does not require an Odoo API key: its selected `odoo.conf`
and detected executable paths remain session-only machine state and are not
stored in this contract.

The lightweight model catalogue, selected technical-model allowlist, and
effective-field catalogue are project evidence stored in DuckDB with target
identity, database, Odoo version, capture time, and content hashes. Opening a
project reuses these snapshots without contacting Odoo. Refreshing is
explicit; changing the model scope invalidates dependent field, governance,
and active mapping state.

## Source-file evidence

Stage A accepts `.csv` and `.xlsx` only. The intake service:

1. rejects paths, control characters, unsupported extensions, empty files, and
   files above the configured bound;
2. streams the upload to a project-local partial file;
3. applies CSV/XLSX container validation in a spawned worker with a timeout and
   hard memory limit;
4. calculates SHA-256;
5. atomically renames it to an application-generated storage name;
6. records immutable evidence in the project database.

Dataset-specific worksheet, header, type, and row inspection remains Stage B.

## Persistence

Development projects live under `var/projects/` when explicitly configured.
The normal Windows launcher uses `%LOCALAPPDATA%\Impodo\projects`.

```text
projects/
├── registry.duckdb
└── <project-uuid>/
    ├── project.duckdb
    ├── inbox/
    ├── staging/
    ├── snapshots/
    ├── reports/
    └── audit/
```

The registry contains only project-list metadata. Each project database holds
its own governed metadata and audit events. Registration evidence is written
to `audit/project-registration-r<revision>.json`.

Application services access source files through the `ArtifactStore` port.
The local adapter materializes contained project files; a future hosted
adapter may materialize a temporary worker-local copy from shared storage.
Repository paths are not part of the application contract.

## Safety boundary

- The browser binds only to an ephemeral IPv4 loopback port.
- State-changing requests require an authenticated launch session, exact
  Origin or Referer, a CSRF token, and non-cross-site Fetch Metadata when that
  optional browser header is present.
- Impodo does not classify targets using organisation-specific lifecycle labels.
- Local mode permits HTTP only for literal `127.0.0.1` or `::1` loopback
  addresses. Remote mode requires HTTPS and rejects loopback targets.
- Connection testing uses only the existing Odoo read connector.
- No browser route creates, writes, unlinks, imports, or executes SQL in Odoo.
