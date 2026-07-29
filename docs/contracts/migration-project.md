# Migration project contract

## Purpose

A migration project is the Phase A governance boundary for every later source,
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
Those transitions must be added before downstream phases depend on them.

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
| Target environment | Optional | `DEV` or `TEST` |
| Odoo base URL | Optional | HTTPS URL |
| Odoo database | Optional | Yes |
| Intended applications | Optional | At least one |
| Intended technical models | Optional | No; confirmed in Stage C |

## System-controlled fields

- UUID project identifier;
- creation, update, and registration timestamps;
- optimistic revision;
- source storage names, byte sizes, SHA-256 hashes, and intake timestamps;
- project status;
- mapping version;
- current run;
- approval status;
- audit-event identifiers and timestamps.

An Odoo API key is not a project field. It is held in memory for the current
process or saved separately in the operating-system credential store.

## Source-file evidence

Phase A accepts `.csv` and `.xlsx` only. The intake service:

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

## Safety boundary

- The browser binds only to an ephemeral IPv4 loopback port.
- State-changing requests require an authenticated launch session, exact
  origin, same-origin Fetch Metadata, and CSRF token.
- Production environments and non-HTTPS Odoo URLs are rejected.
- Connection testing uses only the existing Odoo read connector.
- No browser route creates, writes, unlinks, imports, or executes SQL in Odoo.
