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

**Compiled migration plan**
The immutable, versioned runtime semantics compiled from a browser mapping or
validated YAML `ProfileDocument`. Preparation, staging, request planning,
metadata validation, preflight, and a future package compiler consume this
contract. Canonical staging binds its semantic hash.

**Create**  
A row classification indicating a valid prepared record has no target match.
It is review evidence, not an executed create operation.

**Target fingerprint**
Non-secret evidence identifying the exact Odoo target state relevant to a
snapshot: target hash, connection mode, database identifier, Odoo version,
relevant module versions, and timestamp.

**Field difference**  
One canonical existing/proposed value pair for a comparison field on a record
with exactly one target match.

**Incoming reference**  
A relation whose natural target identity is obtained from another dataset in
the same source package.

**Migration Project**  
The business and governance root for one migration effort. It owns
DataVersion, run, workspace, Recipe-membership, and future CutoverPlan
lineages; it can exist with no Recipe.

**DataVersion**  
One immutable, complete Project source package. It can contain several files
and logical datasets used by different Recipe applications.

**Recipe**  
A Project-scoped reusable identity whose revisions save portable source,
transformation, mapping, target-requirement, and reusable-check meaning. It
does not own source rows, a target, credentials, approvals, or execution.

**Recipe application**  
One use of one exact Recipe revision within a MigrationRun. It owns fresh
current mapping evidence in one isolated workspace.

**MigrationRun**  
One coordinated Authoring, Test, or Production use of one DataVersion and one
target. A multi-Recipe run owns the shared target binding, union requirement
plan, application order, and integrated status.

**MigrationWorkspace**  
An isolated technical working area that references selected DataVersion
datasets and contains current mapping and operational evidence. It is not the
Migration Project.

**TargetBinding**  
Non-secret run evidence identifying one exact Odoo target, purpose, version,
and credential generation. It never contains the credential itself.

**Natural identity**  
An ordered tuple of stable business values used to identify a record without
a target-database-specific ID.

**Odoo ID**  
The numeric primary key of a record in one Odoo database. It is permitted in
target-database-specific snapshots and runtime indexes only.

**Portable**  
Free of target-database-specific numeric IDs and credentials. A portable
result is still bound to the fingerprint and hashes of the target it describes.

**Prepared record**  
A frozen, typed, target-independent representation of one source row,
including proposed scalars, natural-key references, and any preparation
issues.

**Profile**  
A strict YAML declaration describing source layout, target models,
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
itself. It includes the target fingerprint and its snapshot timestamp;
the manifest has no separate run ID or generated timestamp.

**Snapshot**  
Immutable, content-addressed target metadata or record evidence captured
through a read-only connector for one profile requirements plan.

**Target catalog**  
The normalized records retrieved for one Odoo model, indexed by natural
identity, scope, and target-database-specific ID for runtime joins.

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
