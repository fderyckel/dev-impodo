# Profile contract v2

The executable profile contract is defined by strict Pydantic models in
`src/uc_migration_profiler/profile.py`. The authoring reference is
[PROFILE_AUTHORING.md](../../PROFILE_AUTHORING.md), and the canonical starter
is [profiles/template.yaml](../../profiles/template.yaml).

## Contract identity

```yaml
contract_version: 2
profile:
  id: governed_identifier
  version: 2.0.0
  description: Optional description
datasets: []
```

Unknown keys are rejected. Version 1 is accepted only when it uses the same
strict structural shape and version 2 defaults; new profiles use version 2.

## Dataset contract

Each dataset declares:

- stable `name`;
- one CSV source file and encoding/delimiter;
- target model and `upsert`, `create`, or `reference` mode;
- non-empty source identity;
- non-empty target identity components;
- optional target scope;
- optional target-domain restriction;
- scalar mappings;
- relationship mappings.

Dataset names are unique. Incoming references must target a declared dataset.
Dependency cycles are rejected.

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

Version 0.2.0 validates that the value is a YAML sequence but does not
implement the complete Odoo domain grammar. The live server validates domain
semantics. Fixture and saved-snapshot runs assume that the recorded catalog
was already captured with the intended domain.

## Versioning

Increment `profile.version` whenever mapping, identity, scope, type,
normalization, comparison, relation, or domain meaning changes. A future
incompatible YAML structure requires `contract_version: 3`.

Profiles never contain environment URLs, database identifiers, credentials,
tokens, or numeric Odoo record IDs.
