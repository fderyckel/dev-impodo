# Architecture review

**Review date:** 2026-07-28
**Reviewed artifact:** current proof of concept
**Verdict:** the local fixture path is implemented and validated; UC
acceptance remains pending on the larger sanitized slice, live DEV/TEST
evidence, and Odoo-side access controls

This verdict applies only to the read-only preflight component. It is not a
verdict that the end-to-end migration product is complete. Since this review,
Excel/CSV discovery and Phase 2B interactive relationship mapping plus
semantic validation have been implemented and tested separately. Durable
staging, mapping approval, controlled execution, and reconciliation remain
roadmap capabilities documented in
[End-to-end migration product vision](product-vision.md).

## Review scope

The review traced:

- strict profile loading;
- source preparation and canonical types;
- request planning;
- snapshot and JSON-2 connectors;
- metadata validation;
- target catalog indexes;
- relationship resolution;
- target matching and comparison;
- classification precedence;
- portable serialization;
- workbook construction;
- CLI flows, examples, fixtures, and tests.

## Confirmed architecture

| Requirement | Implementation evidence |
| --- | --- |
| Typed prepared values | `canonical.py`, `source.py`, prepared-record tests |
| No IDs in prepared records | logical/business reference value objects |
| Composite/scoped identity | profile components/scope plus target index tuple |
| Incoming references | dependency-ordered dataset resolution |
| Target-only references | preloaded reference catalog lookup |
| Duplicate target preservation | catalog buckets contain all matches |
| Batched access | one request plan per model; paginated target reads |
| Exact field differences | scalar/many2one/many2many comparator |
| Five classifications | fixed precedence in `engine.py` |
| Read-only live surface | three public methods; two allowlisted Odoo methods |
| Portable output | recursive ID rejection and canonical JSON |
| Determinism | stable ordering, typed serialization, input hashes |
| Reviewer workbook | twelve governed sheets built from the JSON result |

## Safety assessment

The connector boundary is capability-limited. It exposes no arbitrary model
method and internally accepts only `fields_get` and `search_read`. The live
configuration rejects production aliases and non-HTTPS URLs. The API key is a
non-repr field and error messages do not include response bodies or secrets.

Defense in depth still requires:

- a dedicated Odoo service user;
- read-only ACLs and record rules;
- a DEV or TEST environment;
- approved host/network policy;
- API key rotation outside this repository.

No implementation claim can replace Odoo-side permissions.

## Data-boundary assessment

`TargetRecord.odoo_id` and relation IDs are confined to record snapshots and
catalog joins. Relation comparison reverse-resolves IDs to natural keys.
Prepared records, decisions, differences, reference evidence, manifest, and
workbook contain business keys.

The manifest serializer recursively rejects keys named `odoo_id`,
`odoo_ids`, `record_id`, or `record_ids`. Golden tests additionally scan for
known target IDs.

## Correctness assessment

Classification order is fail-closed:

1. blocking issue;
2. multiple target matches;
3. no match;
4. one match with differences;
5. one match without differences.

Source duplicates block all duplicate rows. Target duplicates remain visible
and become `AMBIGUOUS`. Missing required incoming or target-only references
become `BLOCKED`. Incomplete record snapshots stop classification.

Scalar comparisons use the profile's type, normalization, precision, and null
policy on both sides. Odoo `false` becomes boolean false only for boolean
fields and null for nullable non-boolean fields. Decimal values use
`Decimal`. Many2many comparison is set-based with explicit operation
semantics.

## Determinism assessment

- source files are SHA-256 hashed;
- saved metadata and record snapshots are SHA-256 hashed;
- CLI-written snapshots include the profile ID and record snapshots include
  source hashes;
- records, decisions, issues, and references have stable ordering;
- portable decimals/dates/datetimes have canonical forms;
- repeated golden execution produces byte-identical manifest JSON.

The generated workbook is a projection of the canonical manifest and is not a
second decision source.

The manifest binds the profile by ID rather than profile-file hash.
Its semantic hash includes source hashes, saved snapshot file hashes, and the
environment fingerprint including snapshot timestamp. Snapshot envelopes do
not yet persist the request domain or a requirements-plan hash.

## Scale assessment

The core uses dictionaries for source identities, target IDs, target business
keys, and references. There is no connector call inside the source-row
classification loop.

The current in-memory design is appropriate for the milestone and preserves
clear substitution points for a future DuckDB/Parquet row store. Actual memory
profiling with the historical 360,000-row source package remains necessary
before production sizing.

The synthetic benchmark exercises a 360,000-key dictionary index; it is not an
end-to-end source/snapshot/workbook benchmark.

## Local verification status

- 46 automated tests pass when the real workbook integration flag is enabled.
- The committed fixture contains 12 import candidates and all five
  classifications.
- The manifest is byte-deterministic for unchanged saved inputs.
- The generated workbook contains and visually exposes all twelve governed
  sheets.
- No live Odoo DEV or TEST call was made as part of this repository-only
  validation.

## Limitations and required partner confirmation

1. The initial target version is confirmed as Odoo 19.4, which provides the
   JSON-2 interface used by this component. The planned Odoo 20.0 move in
   September remains subject to a compatibility check and new DEV/TEST
   acceptance evidence.
2. Confirm the real URL/database routing and whether `X-Odoo-Database` is
   required.
3. Confirm the dedicated account can read `fields_get`, selected target
   records, and relevant module-version records. Add a governed CLI
   configuration for relevant module names.
4. Confirm governed business-key fields and uniqueness scopes for each real
   model.
5. Confirm decimal precision and timezone rules.
6. CSV and XLSX are confirmed for the first source packages and both strict
   adapters are implemented. Legacy XLS and direct connections are deferred.
7. Confirm required Odoo company/context values; the CLI currently supplies an
   empty context.
8. Build and review the planned 100–300-record sanitized UC slice; the current
   semantic fixture has 12 candidates.
9. Decide whether strict snapshot JSON Schema validation plus persisted
   request/domain hashing is required before live acceptance.
10. Confirm the customer's acceptance of the proposed retention and
    access-control policy for snapshots and review packages. The documented
    default is local encrypted, owner-only storage through acceptance plus 90
    days; it is not a substitute for the customer's written policy.

These do not block offline fixture correctness. They block claiming live UC
DEV/TEST acceptance.

## Phase gate

The read-only engine is ready for:

- code review;
- local fixture review;
- configuration with sanitized UC DEV/TEST data;
- Odoo-side read-only access verification.

It is not approval to build or run a write executor. That later milestone must
define approval signatures, staleness, transaction boundaries, idempotency,
retry, reconciliation, and restricted write capabilities separately.
