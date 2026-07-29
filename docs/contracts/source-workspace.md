# Source workspace and mapping-draft contract

## Source confirmation

Each registered file must have a current source catalog before confirmation.
A confirmation records:

- the source-file identifier and immutable SHA-256 hash;
- the exact catalog content hash;
- the effective encoding and delimiter;
- the selected worksheet, named-table, or CSV table keys;
- warning acknowledgement, actor, and timestamp.

Blank or duplicate candidate headers block confirmation. Other warnings require
explicit acknowledgement. Regenerating a catalog invalidates its confirmation
and every downstream source selection and mapping draft.

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
a new version invalidates the mapping draft.

## Odoo schema catalog

Schema discovery is read-only and restricted to the technical models explicitly
permitted during project registration. It performs one batched `fields_get`
request per model, preventing a field- or row-level N+1 pattern.

The catalog contains field label, technical name, type, required/readonly
flags, relation, and selection values. Capture fails closed when the model set,
environment, database, or Odoo 19 version differs from the project boundary.
Recapturing the schema invalidates the mapping draft.

## Mapping draft

The browser maps ordered source columns to writable fields in the captured
schema. A saved mapping is versioned as `DRAFT` or `SUBMITTED` and is bound to
both the frozen-source content hash and schema content hash.

Unknown source columns, unknown or readonly Odoo fields, repeated source
mappings, and repeated target mappings are rejected. `SUBMITTED` currently
means ready for the later semantic-review slice; it is not mapping approval
and grants no Odoo write capability.

Identity, scope, relationship, constant, transformation, import/export, full
semantic validation, and approval remain future Phase 2 slices.
