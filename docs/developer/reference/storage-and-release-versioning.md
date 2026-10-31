---
audience: developer
document_type: reference
status: current
---

# Storage and release versioning

The first-project baseline dated 31 October 2026 assumes that no earlier data
projects need an upgrade. New installations create the complete current schema.
The application continues its existing release numbering.

## Version owners

| Version | Authority | Maintenance rule |
| --- | --- | --- |
| Application release | [`pyproject.toml`](../../../pyproject.toml), currently `26.10.4` | Continue `YY.MM.sequence` and create the matching `vYY.MM.sequence` Git tag when releasing. Synchronize the package entry in `uv.lock` with uv after changing the release version. |
| Release bundle | [`scripts/internal_release.py`](../../../scripts/internal_release.py) | Keep the application version, Git revision, artifact hashes, and validation evidence together in the immutable manifest. |
| Workspace engine | [`domain/shared/schema.py`](../../../src/impodo/domain/shared/schema.py) | Start at version 1 in generation `impodo-workspace-engine-2026-10-31-workspace-owned`. |
| Project registry | [`schema/migration_registry.py`](../../../src/impodo/adapters/duckdb/schema/migration_registry.py) | Start at version 1 in generation `impodo-migration-registry-2026-10-31-project-root`. |
| Data version store | [`schema/data_version_store.py`](../../../src/impodo/adapters/duckdb/schema/data_version_store.py) | Start at version 1 in generation `impodo-data-version-store-2026-10-31-project-owned`. |
| Workspace reference store | [`schema/migration_workspace_store.py`](../../../src/impodo/adapters/duckdb/schema/migration_workspace_store.py) | Start at version 1 in generation `impodo-migration-workspace-2026-10-31-reference-only`. |
| PostgreSQL control schema | [`control_schema.py`](../../../src/impodo/adapters/postgresql/control_schema.py) and [`migrations.py`](../../../src/impodo/adapters/postgresql/migrations.py) | Start at version 1 in generation `impodo-control-2026-10-31`, with one checksummed initial migration. |
| Portable evidence and worker contracts | The owning domain or application module | Keep their current explicit contract versions. These identify serialized meaning and hash inputs, independently of storage layout and release numbering. |

The DuckDB upgrade registries start empty, and new databases have empty
`schema_migration` ledgers. Unsupported generations, future versions, malformed
schemas, and missing upgrade paths are rejected without changing stored data.
The application no longer resets or archives abandoned development layouts.

## Change storage after this baseline

For a DuckDB structural change, increment only the affected store's version,
update its fresh initializer and exact validator, and register a
`ForwardSchemaUpgrade` keyed by the previous version. Keep the generation and
baseline version stable. The shared runner resolves the complete upgrade path
before writing, applies it in one transaction, records each step, and validates
the final shape before committing. Keep local operations within their existing
bounded connection scopes.

For PostgreSQL, append an immutable migration with the next consecutive
version, explicit statements, and a checked-in checksum. Update the schema
contract in that migration and increment the current version in
`control_schema.py`. Never edit an applied migration or derive its contract
parameters from a mutable current-version constant. The migrator retains its
transaction-level advisory lock, checksum validation, and rollback behavior.

Cover fresh creation, repeated opens, upgrades with saved records, failure
rollback, and rejection of unknown contracts. Run focused behavioral tests and
the complete [repository gate](python-code-quality.md) before delivery.

A structural upgrade must not reinterpret hash-bound source packages, Recipes,
approvals, or execution evidence. Change a portable contract deliberately and
preserve its accepted meaning through an explicit supported decoder or an
immutable successor revision.

## Maintain repository scripts

[`scripts/README.md`](../../../scripts/README.md) groups the retained tools by
purpose. Quality gates, release builders, reproducible screenshot capture,
fictional fixture generation, and qualification runners remain useful for new
projects. Remove a script when its responsibility is retired, together with its
callers, tests, and documentation. A date or an old phase name alone does not
make a current verification tool obsolete.
