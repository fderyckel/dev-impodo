# Glossary

**Ambiguous**  
A row classification used only when more than one target record matches the
complete declared target identity and scope.

**Blocked**  
A row classification indicating a source, identity, reference, metadata, or
comparison error prevents a safe proposed action.

**Canonical typed atom**  
The single normalized representation of a scalar business value used across
preparation, snapshots, comparison, hashing, and results.

**Comparison field**  
A target field declared `compare: true`. Differences in these fields decide
between `UPDATE` and `UNCHANGED`.

**Compiled profile**  
The validated frozen `ProfileDocument` loaded from YAML, including dataset
dependencies and field policies. Target requests are derived from it by
planner functions.

**Create**  
A row classification indicating a valid prepared record has no target match.
It is review evidence, not an executed create operation.

**Environment fingerprint**  
Non-secret evidence identifying the Odoo environment state relevant to a
snapshot: alias, database identifier, Odoo version, relevant module versions,
and timestamp.

**Field difference**  
One canonical existing/proposed value pair for a comparison field on a record
with exactly one target match.

**Incoming reference**  
A relation whose natural target identity is obtained from another dataset in
the same source package.

**Natural identity**  
An ordered tuple of stable business values used to identify a record without
an environment-specific database ID.

**Odoo ID**  
The numeric primary key of a record in one Odoo database. It is permitted in
environment-specific snapshots and runtime indexes only.

**Portable**  
Free of environment-specific numeric IDs and credentials. A portable result
is still bound to the fingerprint and hashes of the environment it describes.

**Prepared record**  
A frozen, typed, environment-independent representation of one source row,
including proposed scalars, natural-key references, and any preparation
issues.

**Profile**  
A versioned YAML declaration describing source layout, target models,
identities, scopes, types, relations, and comparison policy.

**Reference catalog**  
Target snapshot records used to map Odoo relation IDs to natural identities
and to resolve target-only source references.

**Review package**  
The canonical JSON preflight result and human-readable workbook for one run.

**Scope**  
Natural identity components, such as company or parent, that bound where a
target identity is unique.

**Semantic hash**  
A digest over the complete portable manifest payload except the hash field
itself. It includes the environment fingerprint and its snapshot timestamp;
the manifest has no separate run ID or generated timestamp.

**Snapshot**  
Immutable, content-addressed target metadata or record evidence captured
through a read-only connector for one profile requirements plan.

**Target catalog**  
The normalized records retrieved for one Odoo model, indexed by natural
identity, scope, and environment-specific ID for runtime joins.

**Target-only reference**  
A relation resolved against records already present in Odoo and not supplied
as an incoming dataset.

**Target requirements plan**  
The deterministic list of models, fields, natural keys, domains, and reference
catalogs derived from a compiled profile and prepared source keys.

**Unchanged**  
A row classification indicating exactly one target match and no difference in
any declared comparison field.

**Update**  
A row classification indicating exactly one target match and one or more
exact field differences. It is review evidence, not an executed write.
