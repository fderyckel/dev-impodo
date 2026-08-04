# Browser workspace contract

## Status and scope

**Status:** Integrated in the local browser.

This contract covers the browser workflow after Project setup:

```text
Source discovery -> Target schema -> Governed mapping
```

Artifacts remain inside one registered migration project and are bound to
immutable source and target evidence.

## Source intake and catalog

Source files can be added only while the project is `DRAFT`. Intake accepts
bounded `.csv` and `.xlsx` files, validates them in a spawned worker, assigns
generated storage names, and records exact byte size and SHA-256 evidence.

After registration, Source discovery creates a profile-free catalog without
changing the source. Each catalog is bound to the file identifier, size,
source hash, contract version, and inspection timestamp.

CSV inspection records encoding, delimiter, candidate header, row shape, and
column profiles. XLSX inspection inventories worksheets, named tables, ranges,
headers, formulas, errors, merged cells, and bounded previews. Type inference
and samples are advisory; digit strings with leading zeroes remain strings.
Formulas are inventoried but never executed or trusted.

Preview, cardinality, row, column, and cell limits prevent inspection from
becoming an unbounded in-memory workload.

## Confirmation and dataset freeze

A source confirmation binds:

- source-file and catalog hashes;
- effective parsing and header settings;
- selected CSV table, worksheet, or named-table keys;
- warning acknowledgement, actor, and timestamp.

Blank or duplicate headers block confirmation. Other warnings require
explicit acknowledgement; acknowledgement proves review, not data quality.

Freezing assigns unique snake-case dataset names and stores versioned dataset
keys, row counts, ordered columns, effective parsing, source/catalog hashes,
actor, timestamp, and a canonical selection hash.

Invalidation is fail-closed:

| Change | Invalidated active evidence |
| --- | --- |
| Reinspect or reconfirm a source | Frozen selection and mapping pointer |
| Freeze a new selection | Mapping pointer |
| Recapture target schema | Schema governance and mapping pointer |
| Change target identity or governed keys | Target-derived mapping and submission evidence |

Historical immutable revisions remain available for audit but are not current.

## Target schema

Target evidence has one explicit origin:

- `LIVE_API`: verified capture from the selected Odoo target;
- `LOCAL_MANUAL`: unverified draft available only for a local project.

Live discovery captures permitted models and their effective Odoo 19 fields,
including type, required/readonly state, relation metadata, inverse field, and
selection values. Abstract and transient models are excluded. The permitted
model set is explicit; related models are not silently added.

Model discovery is paginated and read-only. Field capture performs one
`fields_get` request per selected model, never one call per field or source
row.

A local manual draft requires frozen datasets, an explicit model allowlist,
and acknowledgement that the schema is unverified. It may support mapping
drafts, but mapping submission remains blocked until authenticated live
capture replaces it.

## Schema governance

A user with `schema.govern` confirms versioned natural business keys for each
target model. A definition contains an ordered key, optional company/tenant
scope, description, actor, content hash, and confirmed status. Keys are never
inferred from field names.

Relationships and target matching use governed business keys, not remembered
numeric Odoo IDs.

## Governed mapping

Each frozen dataset declares:

- one permitted target model and `upsert`, `create`, or `reference` mode;
- source trace identity and governed target identity/scope;
- one explicit provider per target scalar field: source, constant,
  source-with-fallback, or leave-unset/Odoo-default;
- allowlisted trim, whitespace, empty-to-null, casing, decimal-locale,
  date-format, boolean, and UTC-datetime transformations;
- required, comparison, validate-only, and null policies;
- many2one/many2many relationships resolved through another dataset or an
  existing-target business key.

One2many is represented through the child dataset's owning many2one. One
source column may feed several explicitly governed target mappings.
Odoo-default intent stays visibly unverified until controlled target rehearsal.

Derived-entity rules may create deterministic related-dataset plans from
denormalized source columns. Their bounded previews do not execute full-row
staging; see [derived-entity authoring](../derived-entity-authoring.md).

## Validation and submission

The pure semantic validator checks hashes, permitted models/fields, governed
identity and scope, type compatibility, readonly/required fields, relation
shape and key arity, provider/transform policies, dependencies, and cycles.
It emits deterministic issues, coverage, deferred checks, and a validation
hash.

Row-level uniqueness, required values, full transformations, and actual
relationship resolution are deferred from mapping validation to the Summary
readiness check. Bounded preview is not clean-package certification.

DuckDB retains append-only mapping revisions, validation results, and
submissions. `SUBMITTED` binds the exact mapping and validation hashes and
requires all blocking issues resolved and warnings acknowledged. It is ready
for the row-level readiness check; it is not approval and grants no Odoo write
capability.

## Row-level readiness

Summary offers one **Check data readiness** action for the current submitted
mapping. It reloads every frozen source row, applies generated parent/child
dataset rules, and checks business keys, values, relationships, and target
matches through the existing preflight engine.

The UI keeps three row-level outcomes: **Ready**, **Needs review**, and
**Blocked**. Status cards and dataset counts filter the source-row table;
plain-language reason and next-action text is shown first, while technical
codes stay collapsed. Target reads are grouped by model, never by source row.
When all rows are ready, the user can generate the Excel review package from
the canonical JSON evidence. All target access remains read-only.
