# Preflight result contract

The canonical output is `uc_preflight_manifest.json`. It is portable: it
contains business keys, typed values, and a separated environment fingerprint,
but no numeric Odoo IDs.

## Envelope

```json
{
  "engine": {"name": "uc-profiler"},
  "profile": {"id": "golden_slice"},
  "source_hashes": {"products.csv": "sha256:…"},
  "snapshot_hashes": {
    "metadata": "sha256:…",
    "records": "sha256:…"
  },
  "target_environment": {},
  "summary": {
    "CREATE": 5,
    "UPDATE": 2,
    "UNCHANGED": 2,
    "AMBIGUOUS": 1,
    "BLOCKED": 2
  },
  "decisions": [],
  "reference_resolutions": [],
  "source_issues": [],
  "metadata_coverage": [],
  "semantic_hash": "sha256:…"
}
```

The semantic hash covers the full payload except itself.

## Decision

```json
{
  "dataset": "products",
  "source_row": 3,
  "business_identity": ["P-UPDATE"],
  "business_scope": [
    {"model": "res.company", "key": ["BE"], "scope": []}
  ],
  "classification": "UPDATE",
  "target_match_count": 1,
  "differences": [
    {
      "dataset": "products",
      "business_identity": ["P-UPDATE"],
      "business_scope": [
        {"model": "res.company", "key": ["BE"], "scope": []}
      ],
      "field": "name",
      "existing": "Old product name",
      "proposed": "New product name",
      "comparison_rule": "string;normalize=trim,collapse_whitespace,empty_as_null;null=distinct",
      "material": true
    }
  ],
  "issues": []
}
```

Every import candidate appears exactly once.

| Classification | Match count | Differences | Blocking issue |
| --- | ---: | ---: | ---: |
| `CREATE` | 0 | 0 | no |
| `UPDATE` | 1 | one or more | no |
| `UNCHANGED` | 1 | 0 | no |
| `AMBIGUOUS` | more than 1 | 0 | target duplicate evidence |
| `BLOCKED` | any | 0 | yes |

Reference-mode datasets are resolution catalogs, not import candidates, and
therefore do not receive decisions.

## Field difference

Each update difference contains:

- dataset and business identity;
- business scope;
- target field;
- existing target business value;
- proposed source business value;
- comparison rule;
- material flag.

Decimal, date, and datetime values use typed portable objects. Relationships
use:

```json
{
  "model": "uom.uom",
  "key": ["KG"],
  "scope": []
}
```

Many2many differences contain canonical business-key sets and the declared
`replace`, `add`, or `remove` rule.

## Reference resolution

Grouped evidence contains dataset, field, logical business reference,
`RESOLVED`, `NOT_FOUND`, `AMBIGUOUS`, or `BLOCKED_BY_DEPENDENCY`, target match
count, and affected source-row count.

No matched target ID is serialized.

## Issues

Issues contain stable code, safe message, severity, dataset, row when singular,
field, and affected count. Classification uses issue severity, never message
text.

## Workbook projection

`uc_preflight_report.xlsx` is derived from this manifest:

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

The workbook contains no independent classification logic. Its Dashboard
totals reference Dataset Summary cells.

## Forbidden content

Serialization fails recursively for keys named:

- `odoo_id`
- `odoo_ids`
- `record_id`
- `record_ids`

Credentials, tokens, cookies, raw authorization headers, arbitrary transport
payloads, and executable Odoo operations are also forbidden.
