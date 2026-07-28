# Examples and edge cases

This guide is the practical companion to the normative contracts. Every
example describes the current proof of concept in this repository. Where a
planned acceptance target is not yet implemented or verified, it is called out
explicitly.

## 1. Complete offline example

Run these commands from `/Users/francois/dev-impodo`.

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler snapshot-metadata \
  --profile profiles/examples/golden_slice.yaml \
  --connector snapshot \
  --snapshot fixtures/golden/target_snapshot.json \
  --output build/golden/metadata.json

PYTHONPATH=src .venv/bin/python -m uc_migration_profiler snapshot-records \
  --profile profiles/examples/golden_slice.yaml \
  --input examples/golden \
  --connector snapshot \
  --snapshot fixtures/golden/target_snapshot.json \
  --output build/golden/records.json

PYTHONPATH=src .venv/bin/python -m uc_migration_profiler preflight \
  --profile profiles/examples/golden_slice.yaml \
  --input examples/golden \
  --metadata build/golden/metadata.json \
  --records build/golden/records.json \
  --output outputs/golden-preflight
```

Expected console summary:

```text
CREATE 5 | UPDATE 2 | UNCHANGED 2 | AMBIGUOUS 1 | BLOCKED 2
```

Expected files:

```text
outputs/golden-preflight/
├── uc_preflight_manifest.json
└── uc_preflight_report.xlsx
```

The compact fixture has 12 import candidates:

| Dataset and business identity | Expected result | Why |
| --- | --- | --- |
| `partners / PARTNER-NEW` | `CREATE` | No target partner has that reference |
| `products / P-CREATE / BE` | `CREATE` | No target product in the `BE` company scope |
| `products / P-UPDATE / BE` | `UPDATE` | Name and tags differ |
| `products / P-SAME / BE` | `UNCHANGED` | Scalar and relation values are canonically equal |
| `products / P-AMB / BE` | `AMBIGUOUS` | Two target products share the complete scoped identity |
| `products / P-BLOCK / BE` | `BLOCKED` | Required UoM business key `MISSING` is unresolved |
| `products / P-SCOPE / BE` | `CREATE` | The same code exists only in a different company scope |
| `assets / ASSET-1` | `UNCHANGED` | One equal target asset |
| `assets / ASSET-2` | `CREATE` | No target asset |
| `asset_lines / ASSET-1 + 1` | `UPDATE` | Decimal quantity differs |
| `asset_lines / ASSET-2 + 1` | `CREATE` | Parent resolves to the incoming asset and no line exists |
| `asset_lines / ASSET-MISSING + 1` | `BLOCKED` | Incoming parent cannot be resolved |

This fixture covers the required semantic shapes, but it is not the planned
100–300-record UC acceptance slice. That larger sanitized slice and live
DEV/TEST runs remain acceptance work.

## 2. Prepared-record example

Prepare the BOM sources without contacting Odoo:

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler profile \
  --profile profiles/examples/bom.yaml \
  --input examples/bom \
  --output build/bom-profile/prepared-records.json
```

A BOM line retains typed values and symbolic references:

```json
{
  "dataset": "bom_lines",
  "source_row": 2,
  "target_model": "mrp.bom.line",
  "source_identity": ["BOM-001-L10"],
  "target_identity": [
    {
      "origin": "incoming",
      "dataset": "bom_headers",
      "key": ["BOM-001"],
      "scope": []
    },
    10
  ],
  "scalar_values": {
    "product_qty": {"type": "decimal", "value": "0.4500"}
  },
  "blocked": false
}
```

No numeric Odoo ID is needed to prepare or inspect this record.

## 3. CSV and XLSX source examples

### CSV source

```yaml
source:
  file: products.csv
  encoding: utf-8-sig
  delimiter: ","
```

The path is relative to `--input`. The first record is the header. Physical
blank lines are skipped; a row containing delimiters and empty fields remains
a data row and will normally fail its required identity checks.

### XLSX source

```yaml
source:
  file: D365 Products.xlsx
  sheet: Released products
  header_row: 3
```

The worksheet is always explicit, even when the workbook has only one sheet.
Rows 1 and 2 may contain export titles; row 3 supplies headers and the first
data row retains worksheet row number 4. Empty worksheet rows are skipped
without changing later source row numbers.

XLSX native booleans, numbers, dates, and datetimes enter the same canonical
value parser used for CSV strings. A workbook date cell may therefore map to a
`date` or `datetime`, while a business code displayed with leading zeroes must
be stored as text or mapped under an explicit conversion rule.

One workbook may supply several datasets by declaring the same file with
different sheet names. Its byte hash is recorded once; the profile binds each
dataset to its worksheet.

### Structural and security edge cases

These fail the command with exit code `3` before Odoo is contacted:

| Input | Current behavior |
| --- | --- |
| `.xls`, `.xlsm`, `.xlsb`, or another extension | Rejected by the strict profile |
| Absolute source path or `../` traversal | Rejected by the strict profile |
| Source file is a symlink | Rejected by the loader |
| XLSX omits `sheet` | Rejected by the strict profile |
| Named worksheet does not exist | Rejected with the available sheet names |
| Blank or duplicate header | Rejected; columns are never silently renamed |
| CSV row has more cells than headers | Rejected with its row number |
| XLSX formula or Excel error cell | Entire source is rejected; no cached result is trusted |
| Encrypted/password-protected workbook | Rejected as an unreadable/encrypted container |
| Macro, external link/connection, or embedded object | Rejected |
| Renamed arbitrary ZIP | Rejected because required XLSX members are absent |
| Unsafe ZIP member or symlink | Rejected |
| File larger than 50 MiB | Rejected |
| XLSX expands beyond 512 MiB or has a suspicious ratio | Rejected |
| More than 10,000 XLSX members | Rejected |
| More than 256 worksheets or oversized Office metadata | Rejected |
| More than 500,000 data rows or 2,048 columns | Rejected |
| Cell string exceeds 1,000,000 characters | Rejected |

CSV cells beginning with formula markers remain inert source strings. If they
later appear in the generated review workbook, the workbook writer forces
them to text. XLSX formula cells are stricter: they are rejected at ingestion.

Legacy `.xls` conversion is intentionally not performed by Impodo. Convert it
to `.xlsx` with an approved desktop tool, preserve the original as governed
evidence, and review any formulas or type changes before using the converted
file.

## 4. Profile examples

### Composite identity

Component order is significant:

```yaml
source_identity:
  fields: [external_line_key]

target_identity:
  components:
    - source_fields: [document_code]
      target_fields: [document_code]
      type: string
      normalize: {trim: true}
    - source_fields: [line_number]
      target_fields: [sequence]
      type: integer
```

`["DOC-1", 10]` and `["DOC-1", 20]` are different target identities.
`source_identity` is the source trace/de-duplication key; it does not have to
equal the target identity.

### Company-scoped identity

```yaml
target_identity:
  components:
    - source_fields: [article_code]
      target_fields: [default_code]
      type: string
      normalize: {trim: true}
  scope:
    - source_fields: [company_code]
      target_fields: [company_id]
      resolve:
        target_model: res.company
        target_fields: [x_uc_code]
```

`P-100 / BE` and `P-100 / FR` are distinct. Scope is shown in decisions and
field differences as a business reference, never as a company database ID.

### Target-only many2one

```yaml
relations:
  uom_id:
    kind: many2one
    source_fields: [uom_code]
    resolve:
      target_model: uom.uom
      target_fields: [x_uc_code]
    required: true
    required_on_create: true
    compare: true
    on_missing: error
    on_ambiguous: error
```

The planner requests `uom.uom.x_uc_code` once for the dataset batch. A unique
`KG` match becomes:

```json
{"model": "uom.uom", "key": ["KG"], "scope": []}
```

### Incoming parent/child relation

```yaml
relations:
  asset_id:
    kind: many2one
    source_fields: [asset_code]
    resolve:
      dataset: assets
      target_source_fields: [asset_code]
    required: true
    compare: true
```

`target_source_fields` must exactly equal the referenced dataset's complete
`source_identity.fields`. A child carries the resolved parent's target
business identity and scope.

### Many2many operations

```yaml
relations:
  tag_ids:
    kind: many2many
    source_fields: [tag_codes]
    separator: ";"
    resolve:
      target_model: product.tag
      target_fields: [x_uc_code]
    compare: true
    operation: replace
```

Given existing `{BLUE, FOOD}`:

| Source | Operation | Final proposed set | Material? |
| --- | --- | --- | ---: |
| `FOOD;BLUE` | `replace` | `{BLUE, FOOD}` | no |
| `GREEN` | `add` | `{BLUE, FOOD, GREEN}` | yes |
| `BLUE` | `remove` | `{FOOD}` | yes |
| empty | `replace` | `{}` | yes |
| empty | `add` or `remove` | `{BLUE, FOOD}` | no |

Input order has no business meaning. An empty item (`BLUE;;FOOD`) or duplicate
item (`BLUE;BLUE`) creates a blocking source issue. The duplicate is
de-duplicated only to keep later evidence deterministic; the row remains
blocked.

### Validate-only field

```yaml
fields:
  legacy_status:
    source: legacy_status
    type: string
    compare: false
    validate_only: true
    required: true
```

The value is parsed and can block preparation, but it never creates a
`FieldDifference`. `validate_only: true` with `compare: true` is rejected.

### Required only when creating

```yaml
fields:
  name:
    source: name
    type: string
    required: false
    required_on_create: true
    compare: true
```

An empty name can still be compared against an existing target. If no target
match exists, the candidate becomes `BLOCKED` with
`REQUIRED_ON_CREATE_MISSING` instead of `CREATE`.

### Create-only and reference datasets

```yaml
target:
  model: x_uc.asset
  mode: create
  on_existing: block
```

With `on_existing: block`, one existing match produces `BLOCKED` and
`CREATE_IDENTITY_EXISTS`. With `on_existing: unchanged`, one existing match
produces `UNCHANGED` without field comparison.

```yaml
target:
  model: res.company
  mode: reference
```

A reference dataset participates in dependency and business-key resolution,
but its rows do not receive classifications.

### Target domain

```yaml
target_domain:
  - [active, "=", true]
  - [company_id, "!=", false]
```

The live planner combines this domain with any single-field source identity
restriction. Local profile validation accepts the YAML list structure but
does not implement a full Odoo-domain grammar; Odoo remains the authority on
domain semantics. A domain that excludes a legitimate match can incorrectly
turn that record into `CREATE`, so domains require business review.

## 5. Canonical-value edge cases

| Input or condition | Implemented behavior |
| --- | --- |
| String with `trim: true` | Leading and trailing whitespace is removed |
| String with `collapse_whitespace: true` | Runs of whitespace become one space |
| String with `casefold: true` | Unicode case-folding is applied to source and target |
| Empty string with `empty_as_null: true` | Becomes null |
| Integer `01` | Becomes integer `1` when used by an integer target component or field |
| Decimal `1.235`, `decimal_places: 2` | Becomes `Decimal("1.24")` using half-up quantization |
| Boolean `false`, `0`, `no`, or `n` | Becomes boolean false |
| Boolean `sometimes` | `SOURCE_TYPE_INVALID`; the row is blocked |
| Date `2026-07-28` | Becomes a date and serializes as a typed date object |
| Datetime with offset | Converts to the equivalent UTC instant |
| Naive datetime with timezone `UTC` | Treated as UTC |
| Naive datetime with another profile timezone | Rejected; the proof of concept does not guess the offset |
| Target Odoo `false` for a boolean | Boolean false |
| Target Odoo `false` for a nullable non-boolean | Null |

Boolean parsing is token-based, not truthiness-based. For example, the string
`"False"` is false; an arbitrary non-empty string is not automatically true.

### Null policies

Assume the source has already passed its `empty_as_null` normalization:

| Policy | Source | Target | Equal? |
| --- | --- | --- | ---: |
| `distinct` | null | empty string | no |
| `equivalent` | null | empty string | yes |
| `ignore_source_null` | null | any target value | yes |

For many2one, `ignore_source_null` preserves the existing reference. For
many2many, an empty CSV cell is represented as an empty set; list operations
therefore follow the operation table above rather than scalar null behavior.

## 6. Classification edge cases

Classification is evaluated in this order:

```text
blocking issue
→ multiple target matches
→ no target match
→ one target match with differences
→ one equal target match
```

| Condition | Result | Evidence |
| --- | --- | --- |
| Invalid source scalar | `BLOCKED` | `SOURCE_TYPE_INVALID` |
| Empty required source value | `BLOCKED` | `SOURCE_REQUIRED_VALUE_MISSING` |
| Null identity component | `BLOCKED` | `SOURCE_IDENTITY_INVALID` |
| Duplicate source trace identity | all duplicates `BLOCKED` | `SOURCE_IDENTITY_DUPLICATE` on every row |
| Missing required relation | `BLOCKED` | `REFERENCE_NOT_FOUND` |
| Ambiguous compared relation | `BLOCKED` | `REFERENCE_AMBIGUOUS` |
| Blocked incoming parent | child `BLOCKED` | `REFERENCE_BLOCKED_BY_DEPENDENCY` |
| Two complete target identity matches | `AMBIGUOUS` | match count retained; no differences |
| No target match, all create requirements present | `CREATE` | match count `0` |
| No target match, create-only required value absent | `BLOCKED` | `REQUIRED_ON_CREATE_MISSING` |
| One target match, material differences | `UPDATE` | one entry per differing field |
| One target match, no material differences | `UNCHANGED` | empty differences |
| Target relation ID missing from the captured reference catalog | `BLOCKED` | `TARGET_REFERENCE_UNRESOLVED` |
| Metadata model/field/type/relation mismatch | affected dataset rows `BLOCKED` | metadata issue and coverage row |

Warnings are possible only for non-compared, non-required relation checks.
They do not change classification. Any relation needed for comparison or
creation must use error policies.

### Source identity versus target identity

Source duplicate detection uses the declared `source_identity` after trimmed
string parsing. Target matching uses the separately typed
`target_identity`. Therefore source keys that are different strings can still
normalize to the same target key—for example source keys `01` and `1` mapped
to an integer target identity. Profiles should choose a source identity that
also prevents this semantic duplicate. The proof of concept does not add a second
duplicate check over canonical target identities.

## 7. Snapshot and connector edge cases

| Condition | Current behavior |
| --- | --- |
| Metadata and record fingerprints differ | Preflight rejects the run |
| Saved record source hashes differ from current CSV/XLSX bytes | Snapshot loading rejects the run when the binding is present |
| Saved profile ID differs | Snapshot loading rejects the run when the binding is present |
| Record snapshot has `complete: false` | Snapshot loading stops; no decisions are produced |
| Metadata snapshot has `complete: false` | A global blocking issue is applied to import candidates |
| Pagination repeats an Odoo ID | Live connector rejects the result as incomplete |
| Final page is exactly full | Connector asks for another page; the empty/short page proves completion |
| HTTP 401 or 403 | Redacted authentication/authorization failure |
| HTTP 429, 502, 503, or 504 | Safe read retries with bounded exponential delay |
| Timeout or unreachable host | Bounded retries, then a redacted transport error |
| Odoo version endpoint unavailable | Fingerprint records version `unknown` plus a non-blocking limitation |
| Module-version read unavailable | Non-blocking limitation, when relevant modules were configured |
| URL is not HTTPS | Configuration is rejected |
| Environment is not `DEV` or `TEST` | Configuration is rejected |
| Redirect changes hostname | Transport rejects it |

Current snapshot limitations:

- snapshot envelopes are loaded from trusted local files and are not yet
  validated by a complete JSON Schema;
- `kind` is written but not strictly rejected when absent or changed;
- profile/source bindings are checked when those fields are present, so
  operators must use snapshots produced by this CLI rather than hand-trimmed
  JSON;
- record snapshots do not persist a requirements-plan hash or requested
  domain;
- `SnapshotConnector` assumes its fixture or saved record snapshot was already
  scoped correctly; it projects fields but does not re-evaluate Odoo domains.

These limitations do not change the deterministic committed fixture result,
but they are reasons not to treat hand-authored snapshots as trusted live
evidence.

### Live module versions and context

`Json2Config` supports `relevant_modules` and an Odoo `context` when it is
constructed in Python. The CLI environment loader does not expose
either setting, so CLI-created live fingerprints currently have an empty
module-version map and use an empty context. Company-specific context must be
added and reviewed before a UC deployment that depends on it.

### Scoped target-only references

`target_scope_fields` are used when reverse-rendering an existing relation as
a business reference. Forward target-only resolution currently looks up only
`target_fields`; if the same reference key exists in multiple scopes, the
result is `AMBIGUOUS`. The current profile shape has no source-side scope
mapping for a target-only relation.

Target-only resolver keys are matched exactly against captured catalog values;
source reference parts are trimmed strings, and resolvers have no independent
type/normalization declaration. Governed Odoo key fields must therefore use
the same canonical text representation. Although `target_scope_fields` are
requested, metadata coverage currently validates only
`target_fields`.

## 8. Metadata edge cases

| Metadata condition | Issue |
| --- | --- |
| Target model unavailable | `TARGET_MODEL_UNKNOWN` |
| Target field unavailable | `TARGET_FIELD_UNKNOWN` |
| Scalar type incompatible | `TARGET_TYPE_INCOMPATIBLE` |
| Relation kind differs | `TARGET_RELATION_KIND_INCORRECT` |
| Related model differs | `TARGET_RELATED_MODEL_INCORRECT` |
| Proposed field is readonly | `TARGET_FIELD_READONLY` |
| One2many is configured as an owned import relation | `TARGET_ONE2MANY_WRITE_OWNER_INVALID` |
| One2many has no inverse field | `TARGET_INVERSE_RELATION_MISSING` |

Selection metadata is captured when Odoo returns it. Decimal comparison
precision is governed by the profile's `decimal_places`; the proof of concept does
not infer digits or rounding from Odoo field metadata.

## 9. Output safety edge cases

- Portable serialization rejects keys named `odoo_id`, `odoo_ids`,
  `record_id`, or `record_ids` anywhere in the manifest.
- Target snapshot IDs are allowed because snapshots are environment-specific.
- Existing relation IDs are reverse-resolved through a business-key catalog
  before they enter a difference.
- A missing reverse-resolution record blocks the affected candidate instead
  of exposing or comparing an ID.
- Workbook cells beginning with `=`, `+`, `-`, or `@` are prefixed as text to
  prevent source-controlled spreadsheet formula execution.
- Date-like strings and dotted numeric-looking strings are also forced to text
  so business keys are not silently reformatted by Excel.
- The workbook contains formulas only for governed Dashboard totals; the
  workbook builder scans for common formula errors before export.

The JSON manifest is the decision source. The workbook is a review projection
and must never be used as an independent classifier.

## 10. Issue-code reference

File/container failures are structural `SourceLoadError` failures and exit
with code `3`; they do not become row-level issue codes. Row-level problems
that survive structural loading use the codes below.

| Code | Meaning |
| --- | --- |
| `SOURCE_FIELD_MISSING` | A mapped CSV/XLSX header is absent |
| `SOURCE_TYPE_INVALID` | A source value or many2many list shape cannot be parsed |
| `SOURCE_REQUIRED_VALUE_MISSING` | A required scalar or relation key is empty |
| `SOURCE_IDENTITY_INVALID` | A source/target identity component is empty or invalid |
| `SOURCE_IDENTITY_DUPLICATE` | The same dataset/source trace identity occurs more than once |
| `SOURCE_REFERENCE_DUPLICATE` | A many2many source list repeats a business key |
| `TARGET_SNAPSHOT_INCOMPLETE` | Metadata evidence is marked incomplete |
| `TARGET_MODEL_UNKNOWN` | A target or reference model is unavailable |
| `TARGET_FIELD_UNKNOWN` | A mapped or reference field is unavailable |
| `TARGET_TYPE_INCOMPATIBLE` | Odoo scalar metadata conflicts with the profile type |
| `TARGET_FIELD_READONLY` | A non-validate-only proposed field is readonly |
| `TARGET_RELATION_KIND_INCORRECT` | Relation metadata is not the declared many2one/many2many kind |
| `TARGET_RELATED_MODEL_INCORRECT` | Relation metadata points at another model |
| `TARGET_ONE2MANY_WRITE_OWNER_INVALID` | The configured field is one2many and cannot own the imported relation |
| `TARGET_INVERSE_RELATION_MISSING` | A detected one2many relation has no inverse field |
| `REFERENCE_NOT_FOUND` | An incoming or target-only business reference has zero matches |
| `REFERENCE_AMBIGUOUS` | A business reference has more than one match |
| `REFERENCE_BLOCKED_BY_DEPENDENCY` | An incoming reference points to a blocked prepared record |
| `TARGET_REFERENCE_UNRESOLVED` | A target-side relation ID or relational identity cannot be rendered as a business key |
| `TARGET_IDENTITY_AMBIGUOUS` | More than one target matches the candidate's complete identity and scope |
| `REQUIRED_ON_CREATE_MISSING` | A zero-match candidate lacks a create-required field/relation |
| `CREATE_IDENTITY_EXISTS` | A create-only dataset with `on_existing: block` found one match |
| `COMPARISON_UNSUPPORTED` | A captured target scalar cannot be normalized under the profile rule |

Record-snapshot incompleteness is normally raised by the connector before
classification and therefore may appear as a process error rather than a
manifest issue. `TARGET_SNAPSHOT_INCOMPLETE` is used for incomplete metadata
evidence.

## 11. Test pointers

| Behavior | Automated evidence |
| --- | --- |
| Types, booleans, null policies, strict profiles | `tests/test_profile_and_values.py` |
| CSV/XLSX safety, prepared records, duplicate source identities, batching, domains | `tests/test_source_and_planner.py` |
| Catalog duplicates, business references, metadata mismatch | `tests/test_catalog_metadata.py` |
| Five classifications, scopes, composite identity, relations, determinism | `tests/test_engine.py` |
| JSON-2 headers, pagination, timeout redaction, closed public surface | `tests/test_connectors.py` |
| CLI and real workbook generation | `tests/test_reporting_cli.py` |

Run all 46 tests, including the workbook integration:

```bash
UC_RUN_WORKBOOK_TESTS=1 \
PYTHONPATH=src \
.venv/bin/python -m unittest discover -s tests -v
```

Live DEV/TEST smoke tests, a 100–300-record sanitized UC slice, memory
profiling of the historical 360,000-row package, and Odoo-side ACL evidence
are not part of the local automated suite.
