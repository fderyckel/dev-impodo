# Source workspace and semantic-mapping contract

## Source confirmation

Each registered file must have a current source catalog before confirmation.
A confirmation records:

- the source-file identifier and immutable SHA-256 hash;
- the exact catalog content hash;
- the effective encoding and delimiter;
- the selected worksheet, named-table, or CSV table keys;
- warning acknowledgement, actor, and timestamp.

Blank or duplicate candidate headers block confirmation. Other warnings require
explicit acknowledgement. Regenerating a catalog invalidates its confirmation,
the active downstream source selection, and the active mapping pointer.
Immutable historical mapping revisions remain retained.

## Frozen dataset selection

After every source is confirmed, the selected tables receive unique,
lowercase snake-case dataset names. Freezing creates a versioned artifact with:

- a stable dataset key derived from the source-file and table keys;
- the source and catalog hashes;
- effective header, encoding, and delimiter;
- row count and ordered source columns;
- a stable column key derived from ordinal and source name;
- actor, timestamp, version, and canonical content hash.

Changing or reconfirming any source invalidates the frozen selection. Freezing
a new version invalidates the active mapping pointer without deleting its
revision, validation, or submission history.

## Odoo model and schema catalogs

Model discovery uses paginated, read-only `search_read` calls against
`ir.model` to capture labels, technical names, defining modules, and model
state. Abstract and transient models are excluded. The Stage A application
scope filters the browser choices, but the explicit Stage C model selection
remains the enforced read boundary.

Field discovery is read-only and restricted to those explicitly permitted
technical models. It performs one batched `fields_get` request per selected
model, preventing a field- or row-level N+1 pattern. Odoo's effective
`fields_get` result includes fields exposed by installed model extensions and
delegation; related models are never added to scope automatically.

The catalog contains field label, technical name, type, required/readonly
flags, relation, inverse `relation_field`, and selection values. Capture fails
closed when the model set, environment, database, or Odoo 19 version differs
from the project boundary. Recapturing the schema invalidates current schema
governance and the active mapping pointer while retaining history.

## Schema governance

Before mapping, a user with `schema.govern` confirms one or more natural
business-key definitions. Each definition records a target model, an ordered
key, optional company/tenant scope, description, and `CONFIRMED` status.
Definitions are never inferred from field names. Schema governance is
versioned, actor-attributed, content-hashed, and bound to the exact captured
catalog.

## Dataset-centric mapping

Each frozen dataset declares:

- one permitted target model and `upsert`, `create`, or `reference` mode;
- source trace identity;
- target identity and scope matching one confirmed business key;
- typed scalar mappings with one explicit source-column, constant,
  source-with-fallback, or leave-unset/Odoo-default provider;
- allowlisted trim, whitespace, empty-to-null, casing, decimal-locale,
  date-format, and UTC datetime transformation policies;
- comparison, required-value, validate-only, and null policies;
- many2one and many2many mappings resolved through an incoming dataset or an
  existing-target business key.

One2many fields are not directly mapped. The browser identifies the captured
inverse field and guides the user to map the child dataset's owning many2one.
Target fields have one provider per dataset; one source column may feed several
explicit mappings.

Constants and fallbacks are stored as raw governed literals and validated
against the captured Odoo field type and selection keys. `odoo_default` means
the future create payload omits that field; it does not call `default_get`.
Because schema metadata cannot prove the runtime default, this provider emits a
warning that must be acknowledged and later verified in DEV/TEST.

The browser preview uses only the bounded, hash-bound samples already captured
during source inspection. It never performs a per-field Odoo query and is not a
replacement for row-level staging validation.

## Semantic validation and submission

`MappingCompiler` canonicalizes order-insensitive collections. The pure
`MappingSemanticValidator` checks exact source/schema hashes, permitted models
and fields, governed identity/scope, type compatibility, readonly and required
fields, relation kind/model/key arity, safe policies, incoming dependencies,
and cycles. It emits deterministic structured issues, coverage, deferred
runtime checks, and a validation hash.

Row uniqueness, row-level required values, and actual reference resolution are
explicitly deferred to staging and preflight; the semantic validator does not
claim they passed.

DuckDB retains append-only mapping revisions, validation results, and
submissions. `SUBMITTED` binds the exact mapping and validation hashes and
requires no blocking issue plus acknowledgement of every current warning. It
means ready for a later approval slice, is not approval itself, and grants no
Odoo write capability.

Governed lookup translations, mapping import/export, functional review, and
mapping approval remain in the later delivery Phase 2C scope.
