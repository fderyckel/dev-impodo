# Profile authoring

Profiles are strict YAML documents. Unknown keys, contradictory settings,
invalid relation origins, inconsistent identity arity, and dependency cycles
fail before source or target processing.

Start from [profiles/template.yaml](profiles/template.yaml).

## Root

```yaml
profile:
  id: products
  description: Governed product preflight
datasets:
  - name: products
    source:
      file: products.csv
    target:
      model: product.template
      mode: upsert
    source_identity:
      fields: [article_code]
    target_identity:
      components:
        - source_fields: [article_code]
          target_fields: [default_code]
          type: string
          normalize: {trim: true}
    fields: {}
    relations: {}
```

Profile IDs must start with a lowercase letter and contain only lowercase
letters, digits, `_`, or `-`. Dataset names follow the same rule except that
`-` is not allowed.

## Dataset

```yaml
- name: products
  source:
    file: products.csv
    encoding: utf-8-sig
    delimiter: ","
  target:
    model: product.template
    mode: upsert
  source_identity:
    fields: [article_code, company_code]
```

Modes:

- `upsert`: classify zero matches as `CREATE`, one match by comparison;
- `create`: zero matches are `CREATE`; `on_existing` must be `block` or
  `unchanged`;
- `reference`: available to other datasets but not an import candidate.

`source.encoding` defaults to `utf-8-sig` and `source.delimiter` defaults to
`,` when omitted.

For XLSX, name the worksheet explicitly:

```yaml
- name: products
  source:
    file: D365 Products.xlsx
    sheet: Released products
    header_row: 3
```

`sheet` is required for `.xlsx` and forbidden for `.csv`. `header_row`
defaults to `1`; a different value is allowed only for `.xlsx`. CSV-only
`encoding` and `delimiter` settings are rejected when explicitly supplied for
an XLSX source.

Only contained relative `.csv` and `.xlsx` paths are valid. Absolute paths,
parent traversal, legacy `.xls`, macro-enabled workbooks, encrypted files,
formulas, Excel error cells, external links/connections, embedded objects,
duplicate or blank headers, and unsafe Office containers are rejected before
record preparation. XLSX blank rows are skipped while `source_row` retains the
actual worksheet row number.

## Target identity and scope

Scalar component:

```yaml
target_identity:
  components:
    - source_fields: [article_code]
      target_fields: [default_code]
      type: string
      normalize:
        trim: true
```

Composite identity adds components in order.

`source_identity.fields` is the trimmed string key used for source
traceability and duplicate detection. It is separate from the typed
`target_identity`. Choose it so that two different source keys cannot
normalize to the same target identity.

Company-scoped identity:

```yaml
target_identity:
  components:
    - source_fields: [article_code]
      target_fields: [default_code]
      type: string
  scope:
    - source_fields: [company_code]
      target_fields: [company_id]
      resolve:
        target_model: res.company
        target_fields: [x_uc_code]
```

Scope values are part of uniqueness and comparison. They are resolved by
business keys.

## Scalar fields

```yaml
fields:
  name:
    source: description
    type: string
    required: true
    required_on_create: true
    compare: true
    validate_only: false
    normalize:
      trim: true
      collapse_whitespace: true
      casefold: false
      empty_as_null: true
    null_policy: distinct
```

Supported types:

- `string`
- `integer`
- `decimal`
- `boolean`
- `date`
- `datetime`

Decimal example:

```yaml
quantity:
  source: source_quantity
  type: decimal
  compare: true
  normalize:
    decimal_places: 4
```

Booleans accept explicit true/false tokens only. Dates use ISO
`YYYY-MM-DD`. Datetimes use ISO-8601 and normalize to UTC; naive datetimes are
accepted only when the profile timezone is `UTC`.

`decimal_places` quantizes with decimal half-up rounding. For example,
`1.235` at two places becomes `1.24`; values never pass through binary
floating point.

`validate_only: true` requires `compare: false`. Such fields are typed and
validated but do not propose a future value or affect `UPDATE`.

Null policies:

- `distinct`: null and empty string differ;
- `equivalent`: null and empty string compare equal;
- `ignore_source_null`: a null source does not change the target.

`empty_as_null` runs before the null policy. To preserve an empty string as
distinct from null, set `normalize.empty_as_null: false`.

Scalar defaults:

| Setting | Default |
| --- | --- |
| `required` | `false` |
| `required_on_create` | `false` |
| `compare` | `true` |
| `validate_only` | `false` |
| `null_policy` | `distinct` |
| `trim` / `collapse_whitespace` / `casefold` | `false` |
| `empty_as_null` | `true` |
| `timezone` | `UTC` |

## Target-only many2one

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
    operation: replace
```

The planner retrieves `uom.uom.x_uc_code` once in a batch. Required or
compared references must use error policies; an unresolved proposed relation
cannot safely continue as a warning.

For a governed reference whose rendered business identity includes scope,
`target_scope_fields` may be added:

```yaml
resolve:
  target_model: x_uc.reference
  target_fields: [code]
  target_scope_fields: [company_code]
```

In the proof of concept these scope fields are used when reverse-rendering an existing Odoo
relation. Forward source-to-target resolution still matches only
`target_fields`; duplicate codes across scopes are therefore ambiguous.

## Incoming-dataset many2one

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

`target_source_fields` must equal the referenced dataset's declared source
identity. The engine resolves the referenced prepared record, then carries its
target model, identity, and scope.

## Many2many

```yaml
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

Operations:

- `replace`: final set equals source set;
- `add`: final set is existing union source;
- `remove`: final set is existing minus source.

Order does not matter. Duplicate or empty source items are validation issues.

Relation defaults:

| Setting | Default |
| --- | --- |
| `compare` | `true` |
| `validate_only` | `false` |
| `required` | `false` |
| `required_on_create` | `false` |
| `on_missing` / `on_ambiguous` | `error` |
| `operation` | `replace` |
| `separator` | `;` |
| `null_policy` | `distinct` |

`many2one` supports only `replace`. `many2many` requires exactly one
list-valued source column. A missing-reference warning is allowed only when
the relation is not compared and is neither required nor required on create;
ambiguity can be a warning only when the relation is not compared.

## Target domain

```yaml
target_domain:
  - [active, "=", true]
  - [company_id, "!=", false]
```

The domain restricts the candidate catalog. Source identity restrictions are
added by the request planner. Do not use a domain that can hide legitimate
identity matches.

The local contract accepts the domain as a YAML list but does not implement a
complete Odoo-domain grammar. Live Odoo validates its semantics. Offline
fixtures and saved record snapshots are assumed to have been captured with
the intended domain; `SnapshotConnector` does not re-evaluate it.

## Profile changes

This proof of concept has one current profile shape and no compatibility
promise for saved profiles. When changing any of the following, update the
profile in place and regenerate prepared records, snapshots, and review
artifacts:

- source/target mapping;
- identity or scope;
- normalization or null behavior;
- comparison participation;
- relationship policy;
- target domain.

Profiles must never contain URLs, database IDs, numeric record IDs,
usernames, passwords, API keys, or tokens.

## Validate and inspect prepared records

```bash
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler profile \
  --profile profiles/examples/products.yaml \
  --input examples/golden \
  --output build/products/prepared-records.json
```

See the complete profiles under [profiles/examples](profiles/examples). For
expected decisions, copy-paste workflows, and failure behavior, see
[docs/examples-and-edge-cases.md](docs/examples-and-edge-cases.md).
