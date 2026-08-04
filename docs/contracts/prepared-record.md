# Prepared record contract

A prepared record is the explicit target-independent boundary after
source parsing. Every nonblank CSV record or XLSX data row produces one
record, including rows with field-level validation errors.

`source_row` starts at `2` for a normal CSV header. For XLSX it is the actual
worksheet row, including any title rows before the configured `header_row`.
Blank XLSX rows are skipped without renumbering later rows.

## Domain shape

```text
PreparedRecord
├── dataset
├── source_row
├── target_model
├── source_identity
├── target_identity
├── target_scope
├── scalar_values
├── references
└── issues
```

Blocking status is derived from issue severity.

## Typed values

In memory:

| Type | Python representation |
| --- | --- |
| string | `str` |
| integer | arbitrary-precision `int` |
| decimal | `Decimal` |
| boolean | `bool` |
| date | `date` |
| datetime | timezone-aware UTC `datetime` |
| null | `None` |

Portable JSON renders decimals, dates, and datetimes as typed objects. Decimal
values never pass through binary floating point.

```json
{
  "string": "P-100",
  "integer": 10,
  "decimal": {"type": "decimal", "value": "1.2400"},
  "boolean": false,
  "date": {"type": "date", "value": "2026-07-28"},
  "datetime": {"type": "datetime", "value": "2026-07-28T10:00:00Z"},
  "null": null
}
```

## Reference intents

Before resolution, relationships are `LogicalReference` values.

Incoming:

```json
{
  "origin": "incoming",
  "dataset": "assets",
  "key": ["ASSET-1"],
  "scope": []
}
```

Target-only:

```json
{
  "origin": "target",
  "model": "uom.uom",
  "target_fields": ["x_external_code"],
  "key": ["KG"],
  "scope": []
}
```

After resolution, both use:

```json
{
  "model": "uom.uom",
  "key": ["KG"],
  "scope": []
}
```

No form contains a numeric Odoo ID.

## Invalid rows

Parsing continues far enough to preserve traceability. Invalid fields receive
null placeholders and structured issues such as:

- `SOURCE_FIELD_MISSING`
- `SOURCE_TYPE_INVALID`
- `SOURCE_REQUIRED_VALUE_MISSING`
- `SOURCE_IDENTITY_INVALID`
- `SOURCE_IDENTITY_DUPLICATE`
- `SOURCE_REFERENCE_DUPLICATE`

Any error-severity issue makes the record blocking. It will later receive a
`BLOCKED` decision if its dataset is an import candidate.

## Invariants

A prepared record:

- carries the selected profile's complete proposed field set;
- preserves validated typed values;
- keeps source, target identity, and scope distinct;
- keeps incoming references symbolic until dependency resolution;
- is represented by a frozen dataclass and treated as immutable;
- is deterministically serializable;
- never contains an environment name, URL, database name, credential, target
  snapshot object, connector, or numeric Odoo ID.

The `profile` CLI command writes a portable projection of prepared records for
inspection.
