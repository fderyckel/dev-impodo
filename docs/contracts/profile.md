# Profile contract

The executable profile contract is defined by strict Pydantic models in
`src/impodo/profile.py`. The authoring reference is
[PROFILE_AUTHORING.md](../../PROFILE_AUTHORING.md), and the canonical starter
is [profiles/template.yaml](../../profiles/template.yaml).

## Profile identity

```yaml
profile:
  id: governed_identifier
  description: Optional description
datasets: []
```

Unknown keys are rejected. The proof of concept supports one current profile
shape and does not include legacy compatibility logic.

## Dataset contract

Each dataset declares:

- stable `name`;
- one contained CSV file or explicit XLSX worksheet;
- target model and `upsert`, `create`, or `reference` mode;
- non-empty source identity;
- non-empty target identity components;
- optional target scope;
- optional target-domain restriction;
- scalar mappings;
- relationship mappings.

Dataset names are unique. Incoming references must target a declared dataset.
Dependency cycles are rejected.

CSV sources may declare `encoding` and a one-character `delimiter`. XLSX
sources must declare `sheet` and may declare `header_row`, which defaults to
`1`. CSV-only settings are invalid on XLSX, and a non-default header row is
invalid on CSV. Only `.csv` and `.xlsx` are accepted; `.xls`, `.xlsm`, and
direct source-system connections are outside the current contract.

The source file path is relative to the selected input directory. Absolute
paths and parent-directory traversal are invalid.

Source identity fields are prepared as trimmed strings and are used for source
traceability and duplicate detection. Target identity components are prepared
with their declared scalar types or resolvers. Profiles should ensure that two
distinct source trace keys cannot collapse to the same canonical target key.

## Scalar contract

Supported types are string, integer, decimal, boolean, date, and datetime.
Every scalar mapping declares source column, type, required behavior,
required-on-create behavior, comparison participation, validation-only
behavior, normalization, and null policy.

Typed values—not raw strings—enter prepared records.

## Identity contract

Identity components map ordered source fields to ordered target fields.
Scalar arity must match. A relational component targets exactly one Odoo
relation field and carries a resolver.

Scope components have the same shape and participate in target uniqueness.

## Resolver contract

Exactly one origin is declared:

```yaml
resolve:
  dataset: parent_dataset
  target_source_fields: [parent_code]
```

or:

```yaml
resolve:
  target_model: x.reference
  target_fields: [x_business_code]
  target_scope_fields: []
```

Incoming `target_source_fields` must equal the referenced dataset's complete
source identity. Target-only fields become batched catalog requirements.

## Relation contract

Relations support `many2one` and `many2many`.

Common behavior:

- `required`;
- `required_on_create`;
- `compare`;
- `validate_only`;
- `on_missing`;
- `on_ambiguous`;
- `null_policy`.

Many2many additionally supports an explicit source separator and `replace`,
`add`, or `remove`. Compared or required relations must fail on missing
references. Compared relations must fail on ambiguity.

## Target-domain contract

`target_domain` contains an Odoo domain. The planner combines it with bounded
source identity/reference restrictions. It must not intentionally hide
legitimate identity matches.

The current proof of concept validates that the value is a YAML sequence but
does not implement the complete Odoo domain grammar. The live server validates
domain semantics. Fixture and saved-snapshot runs assume that the recorded
catalog was already captured with the intended domain.

## Change policy

There is one current profile shape. When mapping, identity, scope, type,
normalization, comparison, relation, or domain meaning changes, update the
profile in place and regenerate its prepared records, snapshots, and review
artifacts.

Profiles never contain environment URLs, database identifiers, credentials,
tokens, or numeric Odoo record IDs.
