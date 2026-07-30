# Read connector contract

## Design intent

`OdooReadConnector` is a capability boundary, not a generic Odoo client. It
exposes only the reads the preflight application needs and returns normalized
snapshot-domain objects.

The name describes the port. Implementations are:

- `SnapshotConnector` — reads committed or generated JSON fixtures;
- `Json2ReadConnector` — reads an approved Odoo DEV or TEST environment.

Both are interchangeable from the application layer upward.

## Interface

Illustrative typed Python:

```python
from typing import Protocol, Sequence


class OdooReadConnector(Protocol):
    def get_environment_fingerprint(self) -> EnvironmentFingerprint: ...

    def get_model_metadata(
        self, requests: Sequence[MetadataRequest],
    ) -> MetadataSnapshot: ...

    def get_records(
        self, requests: Sequence[RecordRequest],
    ) -> RecordSnapshot: ...
```

Requests are frozen values produced by the profile-derived planner, not
arbitrary CLI field lists.

The interface deliberately has no:

- `create`;
- `write`;
- `unlink`;
- import;
- arbitrary `call`, `execute`, `execute_kw`, SQL, or server action;
- context option capable of triggering business mutations.

A live transport adapter may internally use a low-level library, but it must
not expose that library or arbitrary method execution through the connector.

## Fingerprint behavior

The adapter configuration contains the DEV/TEST environment label, database,
base URL, credentials, timeout, page size, and optional programmatic context
and module names. The response contains the fields defined in the
[snapshot contract](snapshots.md). The base URL and credential remain
adapter-private.

## Metadata request

Each `MetadataRequest` contains an exact model and field tuple. The JSON-2
adapter always asks `fields_get` for the fixed attributes needed by the
metadata validator. Missing models and fields are detected by application
validation and become structured issues.

## Record catalog request

Each `RecordRequest` contains:

- model;
- exact projected field tuple;
- compiled Odoo domain.

The connector supplies deterministic `id asc` ordering and its configured page
size. The proof of concept does not persist a request hash, page evidence, or domain
in the saved record snapshot, and does not split very large `in` domains into
transport-sized chunks.

## Connector behavior

All connectors:

- use stable ordering;
- project only requested fields into returned records;
- return no partial catalog as if it were complete;
- do not log secrets or response bodies;
- retry only idempotent reads.

The connector retains Odoo's raw field encodings. The application canonicalizes
them using profile rules and captured metadata during catalog construction and
comparison.

## `SnapshotConnector`

Responsibilities:

- compute content hashes for saved files;
- verify matching fingerprints;
- verify profile and source bindings when present;
- project normalized fixture/snapshot JSON into the same domain objects as the
  live connector;
- make no network calls;
- reject incomplete record snapshots.

It is the reference implementation for domain development. All classifier and
report tests run through it. It assumes fixture records were already scoped
for their intended domain and does not re-evaluate Odoo domains locally.

## `Json2ReadConnector`

Responsibilities:

- load base URL/database configuration from environment variables;
- obtain the API key from `IMPODO_ODOO_API_KEY`;
- translate requirements into narrowly projected, paginated reads;
- capture required model metadata;
- enforce HTTPS except for explicitly enabled literal-loopback local mode,
  DEV/TEST environment selection, timeout, deterministic ordering, retry
  rules, and rejection of redirects before credentials can be forwarded;
- write no artifact containing credentials or authorization state.

The exact JSON-2 endpoint and authentication shape must be implemented against
the official API contract for the supported Odoo version. Transport details
must not leak into domain objects.

The service account should also be denied write permissions by Odoo ACLs.
Application-level omission of write methods is the first boundary, not the
only boundary.

## Error model

Connector exceptions are typed:

- `ConnectorConfigurationError`
- `ConnectorAuthenticationError`
- `ConnectorTransportError`
- `ConnectorIncompleteResultError`

Messages are safe for console display and never include secrets or response
bodies containing business data. HTTP 401 and 403 currently share the
authentication/authorization error type.

Transport failure stops snapshot creation. It does not turn missing target
evidence into `CREATE`.

## Automated and live evidence

Local automated tests cover JSON-2 endpoint construction, named arguments,
headers, pagination, timeout redaction, credential-safe representation, and
the absence of public write/generic-call methods. Fixture-backed integration
tests cover field projection and the complete classification path.

No live Odoo call is part of the local suite. Before deployment acceptance, run in an
isolated DEV and TEST environment with a dedicated read-only account and
compare a sentinel record's write timestamp before and after the suite.
