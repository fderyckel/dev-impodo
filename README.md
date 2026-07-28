# UC Migration Profiler

This proof of concept is a model-agnostic, read-only Odoo preflight engine. It
prepares governed source records, captures narrowly scoped Odoo evidence,
resolves business-key relationships, compares source and target values, and
produces an Excel review workbook plus a portable JSON manifest.

It never writes to Odoo.

This proof of concept is the safety and comparison foundation of a larger
end-to-end migration product for Excel and CSV exports from AX 2012, Dynamics
365, Salesforce, and other systems. The complete goal includes source
inspection, Odoo schema discovery, a guided mapping workspace, normalization
and validation, durable staging, approval, controlled Odoo loading, and
reconciliation. See
[End-to-end migration product vision](docs/product-vision.md).

## Milestone status

Implemented:

- one strict current profile shape;
- profile-declared CSV and XLSX worksheets with exact source hashes;
- contained source paths, bounded Office containers, XML-bomb protection,
  duplicate-header checks, and formula/error-cell rejection;
- typed prepared records for strings, integers, decimals, booleans, dates,
  datetimes, and nulls;
- composite and company/site/parent-scoped identities;
- incoming-dataset and target-only relationship resolution;
- many2one and many2many `replace`, `add`, and `remove` comparison;
- profile-derived metadata and record request planning;
- deterministic fixture snapshots;
- Odoo 19 JSON-2 `fields_get` and paginated `search_read`;
- `CREATE`, `UPDATE`, `UNCHANGED`, `AMBIGUOUS`, and `BLOCKED`;
- grouped issue and reference-resolution evidence;
- field-level differences expressed through business keys;
- twelve-sheet business-review workbook;
- deterministic JSON manifest with source and snapshot hashes;
- a compact 12-candidate offline golden slice covering standard,
  extended-standard, and custom models plus parent/child records.

Explicitly excluded:

- Odoo create, write, unlink, import, arbitrary RPC, or SQL;
- production execution;
- an approval or write manifest;
- retry/reconciliation of writes;
- SharePoint, SPFx, Power Apps, and Power Automate.

## Safety boundaries

- The public connector has only three capabilities: environment fingerprint,
  model metadata, and record reads.
- The JSON-2 adapter allowlists only `fields_get` and `search_read`.
- Live execution is rejected unless `UC_ODOO_ENVIRONMENT` is `DEV` or `TEST`.
- Live URLs must use HTTPS.
- Credentials come from environment variables and are redacted from object
  representations and errors.
- Connector requests are planned per model and field set, never per row.
- Numeric Odoo IDs exist only in target snapshots and in-memory catalogs.
- The portable manifest recursively rejects Odoo-ID fields.
- Missing evidence, duplicate identities, and unresolved required references
  fail closed.

## Project layout

```text
src/uc_migration_profiler/   Domain engine, connectors, CLI, and reporting
profiles/                    Profile template and examples
examples/                    CSV/XLSX source-package examples and fixtures
fixtures/                    Normalized offline Odoo snapshots
tests/                       Unit, contract, integration, and golden tests
docs/                        Detailed architecture and contracts
outputs/                     Generated review packages, ignored by Git
```

See [ARCHITECTURE.md](ARCHITECTURE.md) and
[PROFILE_AUTHORING.md](PROFILE_AUTHORING.md).

## Setup

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

CSV ingestion uses Python's standard library. XLSX ingestion uses `openpyxl`
in read-only mode with `defusedxml`; both are installed with the package.

The Excel report writer uses `@oai/artifact-tool` through Node.js. In the
Codex desktop runtime, expose the bundled runtime:

```bash
export UC_NODE_BINARY=/path/to/bundled/node
export UC_ARTIFACT_TOOL_NODE_MODULES=/path/to/bundled/node_modules
```

If `node` and a project-level `node_modules/@oai/artifact-tool` are already
available, those environment variables are unnecessary.

No Odoo credentials are needed for tests or the offline example.

## Run the tests

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Include the real workbook integration test:

```bash
UC_RUN_WORKBOOK_TESTS=1 \
PYTHONPATH=src \
.venv/bin/python -m unittest discover -s tests -v
```

## Existing source-profile command

The original command remains available:

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler profile \
  --profile profiles/examples/bom.yaml \
  --input examples/bom \
  --output build/bom-profile/prepared-records.json
```

This prepares and validates sources without contacting Odoo.

## Complete offline preflight

Capture normalized fixture metadata:

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler snapshot-metadata \
  --profile profiles/examples/golden_slice.yaml \
  --connector snapshot \
  --snapshot fixtures/golden/target_snapshot.json \
  --output build/golden/metadata.json
```

Capture only planned target records and fields:

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler snapshot-records \
  --profile profiles/examples/golden_slice.yaml \
  --input examples/golden \
  --connector snapshot \
  --snapshot fixtures/golden/target_snapshot.json \
  --output build/golden/records.json
```

Run comparison entirely offline:

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler preflight \
  --profile profiles/examples/golden_slice.yaml \
  --input examples/golden \
  --metadata build/golden/metadata.json \
  --records build/golden/records.json \
  --output outputs/golden-preflight
```

Outputs:

```text
outputs/golden-preflight/
├── uc_preflight_manifest.json
└── uc_preflight_report.xlsx
```

The golden slice currently produces:

| Classification | Count |
| --- | ---: |
| `CREATE` | 5 |
| `UPDATE` | 2 |
| `UNCHANGED` | 2 |
| `AMBIGUOUS` | 1 |
| `BLOCKED` | 2 |

See [Examples and edge cases](docs/examples-and-edge-cases.md) for the
record-by-record explanation, profile patterns, expected JSON shapes, failure
behavior, and known boundary cases.

## Live Odoo 19 read-only workflow

Odoo 19 introduced the external JSON-2 endpoint. Configure a dedicated
least-privilege read account:

```bash
export UC_ODOO_BASE_URL=https://odoo-dev.example.com
export UC_ODOO_DATABASE=uc_dev
export UC_ODOO_API_KEY=secret-from-an-approved-secret-store
export UC_ODOO_ENVIRONMENT=DEV
export UC_ODOO_TIMEOUT_SECONDS=30
export UC_ODOO_PAGE_SIZE=500
```

Capture live metadata:

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler snapshot-metadata \
  --profile profiles/examples/golden_slice.yaml \
  --connector json2 \
  --output snapshots/dev-metadata.json
```

Capture live records:

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler snapshot-records \
  --profile profiles/examples/golden_slice.yaml \
  --input examples/golden \
  --connector json2 \
  --output snapshots/dev-records.json
```

Then use the same offline `preflight` command. The implementation follows
Odoo's official
[External JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html):
`POST /json/2/<model>/<method>`, bearer authorization, optional
`X-Odoo-Database`, named JSON arguments, deterministic `id asc` ordering, and
pagination.

## Workbook contents

- Dashboard
- Target Environment
- Dataset Summary
- Proposed Creates
- Proposed Updates
- Field Differences
- Unchanged
- Ambiguous Matches
- Blocked Records
- Reference Resolution
- Source Issues
- Metadata Coverage

Identifiers and relationships are rendered through business keys. Numeric
Odoo IDs are hidden.

## Performance

Preparation, duplicate detection, target matching, and reference resolution use
indexed dictionaries. Remote-call count scales with models, planned domains,
and pages—not source rows.

Run the non-gating synthetic benchmark:

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler benchmark \
  --rows 360000
```

The current implementation keeps prepared and target records in memory.
DuckDB/Parquet is a documented future substitution point if measured memory
use at larger scales requires it; it is not needed for this milestone.

## Current limitations

- Source adapters accept `.csv` and `.xlsx` only. XLSX input requires an
  explicit worksheet and rejects formulas, error cells, macros, external
  links/connections, embedded objects, encryption, and suspicious Office
  containers. Legacy `.xls` and direct source-system connections are deferred.
- The current XLSX capability reads a profile-declared sheet; the planned
  browser workspace will add workbook inventory, preview, type inference, and
  guided selection before a profile exists.
- The live connector targets Odoo 19 JSON-2. Earlier Odoo versions need a
  separately reviewed read adapter.
- Module version visibility is best-effort; access denial is recorded as a
  limitation rather than invalidating otherwise complete metadata. The
  programmatic connector accepts relevant module names, but the current CLI
  environment loader does not yet expose that list.
- Snapshot domains are optimized for single-field identities. Composite keys
  remain grouped in one model request, but may retrieve a broad candidate set
  and can become an unbounded model read when no `target_domain` is supplied.
  Govern and volume-test those profiles before live use.
- Snapshot files written by the CLI carry profile and source bindings, but
  the proof of concept does not persist a requirements-plan hash or requested domain
  and does not apply a full JSON Schema on load. Do not hand-edit live
  evidence files.
- The committed fixture has 12 candidates. The planned 100–300-record
  sanitized UC acceptance slice, live DEV/TEST smoke runs, Odoo-side
  read-only ACL evidence, and historical 360,000-row memory profiling remain
  to be completed.
- No result authorizes a write. A changed target requires a fresh snapshot and
  preflight.

## Documentation

The complete index is at [docs/README.md](docs/README.md).
