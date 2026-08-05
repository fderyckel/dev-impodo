# Profile authoring guide

## Scope

Profiles configure the expert CLI preflight engine. They are strict YAML and
are not generated from browser mapping revisions. Start with
[`profiles/template.yaml`](../../profiles/template.yaml), then validate the
profile with the CLI before capturing Odoo evidence.

A profile must contain business keys and symbolic relationships. Never put an
Odoo URL, database, credential, API key, or numeric Odoo record ID in it.

## Minimal structure

```yaml
profile:
  id: products_v1
  description: Governed product migration profile

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
          normalize:
            trim: true
    fields:
      name:
        source: description
        type: string
        required: true
        required_on_create: true
        compare: true
        normalize:
          trim: true
          collapse_whitespace: true
```

Unknown keys, duplicate dataset names, invalid relations, inconsistent key
arity, and dependency cycles are rejected.

## Dataset source and mode

For CSV, declare `source.file`. For XLSX, also declare the explicit worksheet
required by the schema; do not rely on an active or first sheet.

Choose one target mode:

- `upsert`: create when missing; compare one existing match.
- `create`: create when missing and apply the declared existing-record policy.
- `reference`: resolve relationships without producing an import decision.

Use one dataset for one governed entity and target model. Split unrelated
entities instead of creating one wide, ambiguous profile.

## Identity and scope

`source_identity.fields` makes each source row traceable. `target_identity`
defines the business key used to find an Odoo record.

```yaml
source_identity:
  fields: [article_code, company_code]

target_identity:
  components:
    - source_fields: [article_code]
      target_fields: [default_code]
      type: string
      normalize:
        trim: true
  scope:
    - source_fields: [company_code]
      target_fields: [company_id]
      resolve:
        target_model: res.company
        target_fields: [x_external_code]
```

Use `scope` when a key is unique only within a company, parent, site, or other
business boundary. Prefer stable external codes or governed natural keys.
Names are usually poor identifiers, and database IDs are not portable.

## Scalar fields

Declare only fields needed for validation or comparison.

This section describes expert YAML. For the browser controls and examples, use
the [scalar mapping reference](01-local-browser-user-guide.md#scalar-fields-choose-what-impodo-should-do).

```yaml
fields:
  active:
    source: active
    type: boolean
    required: true
    compare: true

  list_price:
    source: sales_price
    type: decimal
    compare: true
    null_policy: distinct
```

Useful controls include:

- `required`: the source value must be present;
- `required_on_create`: creation needs the value;
- `compare`: include it in change classification;
- `validate_only`: validate without proposing a target change;
- `normalize`: apply declared string normalization;
- `null_policy`: keep null comparison explicit.

Supported portable types include string, integer, decimal, boolean, date, and
timezone-aware datetime. Use decimal for business quantities and prices; do
not route them through binary floating point.

## Relationships

Resolve relationships through business keys.

```yaml
relations:
  uom_id:
    kind: many2one
    source_fields: [uom_code]
    resolve:
      target_model: uom.uom
      target_fields: [x_external_code]
    required: true
    required_on_create: true
    compare: true
    on_missing: error
    on_ambiguous: error
    operation: replace
```

For many-to-many input, declare the separator and operation:

```yaml
relations:
  tag_ids:
    kind: many2many
    source_fields: [tag_codes]
    separator: ";"
    resolve:
      target_model: x_uc.tag
      target_fields: [x_external_code]
    compare: true
    on_missing: error
    on_ambiguous: error
    operation: replace
```

Use `replace`, `add`, or `remove` deliberately. Missing or ambiguous
references should normally be errors; relaxing either policy needs an
explicit business decision.

Incoming references to another source dataset remain symbolic until the
dependency graph is resolved. Keep every referenced dataset in the same
profile and avoid dependency cycles.

## Target domains

Use `target_domain` only for a stable, governed restriction that is part of
the matching rule. A domain must not hide duplicates that should be reviewed.
Keep it small and auditable.

```yaml
target_domain:
  - [active, "=", true]
```

## Authoring workflow

1. Copy the template and give the profile a versioned, meaningful ID.
2. Add one dataset and its business identity.
3. Add the smallest useful scalar and relationship mappings.
4. Run `impodo-cli profile` against representative source files.
5. Fix all schema, type, duplicate-identity, and relationship issues.
6. Review the generated prepared-record JSON before any live snapshot.
7. Add edge cases to the maintained examples or tests when the rule is
   reusable.

The runnable profiles in [`profiles/examples`](../../profiles/examples) show
products, bills of materials, and the golden slice. The normative invariants
and classification behavior are in the
[preflight contract](../contracts/04-preflight.md); execution is covered by the
[CLI runbook](04-cli.md).
