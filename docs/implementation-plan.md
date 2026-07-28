# Implementation plan and status

## Current status

Version `0.2.0` is implemented in `/Users/francois/dev-impodo`. The referenced
scratch-workspace baseline was unavailable in this workspace, so the engine was
implemented from the supplied milestone brief and contracts. The current suite
has 42 tests when the real-workbook integration is enabled. The original BOM
preparation command remains a compatibility check.

This document separates completed local engineering from the evidence still
required for UC acceptance.

## Actual package shape

```text
src/uc_migration_profiler/
├── __init__.py
├── __main__.py
├── canonical.py
├── catalog.py
├── cli.py
├── connectors.py
├── engine.py
├── metadata.py
├── models.py
├── planner.py
├── profile.py
├── reporting.py
├── source.py
└── resources/
    └── build_review_workbook.mjs

profiles/
├── template.yaml
└── examples/

examples/
├── bom/
└── golden/

fixtures/
└── golden/
    └── target_snapshot.json

tests/
├── test_catalog_metadata.py
├── test_connectors.py
├── test_engine.py
├── test_profile_and_values.py
├── test_reporting_cli.py
└── test_source_and_planner.py
```

The implementation uses focused modules rather than separate `domain`,
`application`, `ports`, and `adapters` packages. The important dependency
boundaries remain: engine/domain code does not depend on the CLI, HTTP
transport, or workbook builder; reporting consumes completed results.

## Delivery slices

### Slice 0 — Baseline and compatibility

Completed:

- versioned Python package and editable installation;
- exact setup and test commands;
- original `profile` command preserved;
- BOM example prepares three typed records;
- package version set to `0.2.0`;
- all repository files remain under `/Users/francois/dev-impodo`.

External note: the original five-test scratch baseline was not available for a
literal before/after run.

### Slice 1 — Canonical values and prepared records

Completed:

- string, integer, `Decimal`, boolean, date, datetime, and null values;
- string trim, whitespace collapse, case-folding, and empty-as-null;
- decimal half-up quantization;
- explicit boolean tokens;
- UTC datetime normalization;
- frozen `PreparedRecord`;
- typed scalar mappings retained after validation;
- logical references retained after validation;
- source duplicate detection;
- portable prepared-record projection without target IDs.

Verified examples:

- `1.235` at two decimal places becomes `1.24`;
- `"false"` becomes boolean false rather than truthy text;
- the BOM quantity remains `Decimal("0.4500")`;
- all duplicate source trace keys are blocked.

### Slice 2 — Strict profile contract and request planning

Completed:

- strict Pydantic v2 shape;
- compatibility acceptance of v1 only with the same structural shape;
- profile, dataset, field, identity, scope, relation, and domain declarations;
- create/upsert/reference modes;
- incoming dependency validation and cycle rejection;
- deterministic metadata and record request functions;
- batched requests grouped by target model;
- single-field source/reference `in` domains.

Known limits:

- no persisted requirements-plan hash;
- no complete local parser for Odoo domain grammar;
- composite identities can require a broader profile-domain query;
- very large `in` domains are not split into smaller chunks.

### Slice 3 — Snapshots and target catalog

Completed:

- deterministic combined fixture support;
- separate metadata and record snapshot writers/loaders;
- exact saved-file content hashes;
- fingerprint comparison;
- profile/source binding checks when fields are present;
- incomplete-record rejection;
- duplicate-preserving catalog indexes;
- target ID-to-business-key reverse lookup.

Known limits:

- no complete JSON Schema enforcement on load;
- contract version and `kind` are not strictly required;
- missing optional envelope bindings can pass;
- request domain and requirements hash are not stored;
- fixture/saved snapshot domains are not locally re-evaluated.

### Slice 4 — Resolution, matching, comparison, classification

Completed:

- dependency-ordered incoming relation resolution;
- target-only catalog resolution;
- grouped resolution root causes;
- scalar and relational identity matching;
- scope as part of the target key;
- scalar, many2one, and many2many comparison;
- `replace`, `add`, and `remove`;
- exact field differences;
- create-only existing-identity policies;
- reference-only datasets;
- fixed fail-closed classification precedence;
- five-count reconciliation.

Verified golden totals:

| Result | Count |
| --- | ---: |
| `CREATE` | 5 |
| `UPDATE` | 2 |
| `UNCHANGED` | 2 |
| `AMBIGUOUS` | 1 |
| `BLOCKED` | 2 |

Known limits:

- source duplicate detection uses the source trace identity, not the
  separately typed target identity;
- forward target-only resolution cannot currently include a source-side scope
  key.

### Slice 5 — Portable manifest and review workbook

Completed:

- canonical JSON manifest;
- engine/profile/environment/input bindings;
- semantic hash;
- portable business-key decisions and differences;
- recursive forbidden-ID-key check;
- twelve governed workbook sheets;
- frozen headers, filters, widths, status colors, and Dashboard chart;
- formula-injection protection for source-controlled strings;
- workbook formula-error inspection;
- optional PNG sheet previews.

Known limit: the manifest is finalized before workbook creation, so a workbook
failure can leave a manifest-only directory. Operators must require both
files.

### Slice 6 — Live JSON-2 read adapter

Completed and locally mocked:

- Odoo 19 JSON-2 endpoint shape;
- bearer and database headers;
- `fields_get`;
- projected, paginated `search_read`;
- deterministic `id asc`;
- HTTPS and DEV/TEST restrictions;
- timeouts and bounded transient-read retries;
- cross-host redirect rejection;
- redacted errors and API-key representation;
- public surface containing only three read capabilities.

Pending live evidence:

- real UC DEV and TEST connectivity;
- service-account ACL and record-rule confirmation;
- sentinel write-timestamp comparison;
- database routing and company context;
- relevant UC module list exposed through governed CLI configuration;
- fixture-equivalent live decisions.

### Slice 7 — Acceptance hardening

Partially completed:

- compact 12-candidate semantic fixture;
- non-gating 360,000-key dictionary benchmark;
- operator commands and runbook;
- known limitations and edge-case guide.

Still required:

- sanitized 100–300-record UC golden slice;
- live DEV and TEST runs;
- end-to-end scale, memory, snapshot-size, and workbook timing evidence;
- retention and access policy;
- strict snapshot trust decision;
- reviewed release notes and partner confirmations.

## Pull-request boundaries for later work

If this repository is placed under change review, keep the remaining work
separate:

1. strict snapshot envelope/request binding;
2. large-domain batching and scale controls;
3. CLI context/module configuration;
4. optional XLSX source adapter;
5. live DEV/TEST evidence and acceptance fixtures.

Do not combine any of those with a future write executor.

## Local definition of done

The local 0.2.0 implementation is complete when:

- contracts describe actual behavior;
- all 42 tests, including the workbook integration, pass;
- BOM preparation succeeds;
- offline golden preflight produces all five classifications;
- every update has exact business-key differences;
- missing relations block and target duplicates are ambiguous;
- no public connector write capability exists;
- portable output contains no numeric Odoo IDs;
- repeated fixed-input output is byte-identical;
- generated workbook and manifest reconcile by construction.

## UC acceptance definition

Do not claim UC environment acceptance until:

- the same governed profile path succeeds with fixture, DEV, and TEST
  snapshots;
- the 100–300-record sanitized slice is reviewed;
- Odoo-side read-only controls are evidenced;
- live context, module, key, scope, decimal, and timezone assumptions are
  confirmed;
- expected-scale memory and runtime are measured;
- retention/access rules are approved.

Starting an approval or write phase is outside both definitions.
