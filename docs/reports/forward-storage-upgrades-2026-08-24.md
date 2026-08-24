---
audience: developer
kind: report
status: current
---

# Forward storage upgrades

**Historical evidence:** This dated report records the implementation of the
first forward-compatible Project storage release. Current architecture,
decisions, and lifecycle contracts own behavior.

## Outcome

Impodo now carries recognized databases from version 1 to version 2 within
each current storage generation. The Project registry, DataVersion package,
MigrationWorkspace reference store, and workspace engine each own an isolated
upgrade registry. Version 2 adds the common `schema_migration` evidence table;
it does not rewrite domain rows or hash-bound artifacts.

Normal repositories still implement one current schema. A store opener reads
the schema identity, resolves the complete version path, applies all steps in
one DuckDB transaction, validates the exact current shape, and commits before
returning the database. A later open validates the current shape and does not
reapply the migration.

## Failure and release boundaries

An interrupted step rolls back every structural write, migration record, and
version update made in that database. If interruption happens between
different database files, committed files remain current and untouched files
remain valid version 1 stores; later authorized opens resume them.

The implementation rejects another generation, a version below the supported
baseline, a version newer than the running application, a missing consecutive
step, or a malformed current shape. It contains no downgrade, semantic
backfill, old-field repository branch, source-row migration loop, Odoo call,
dual read, or dual write.

The release test pins each version 1 schema with a deterministic fingerprint
and checks that every version constant has a consecutive registered path. It
also proves multi-step ordering and all-step rollback when a later step fails.

## Verification evidence

- `tests.test_forward_upgrade_compatibility`: 9 tests passed.
- `tests.test_workspace_schema_contract` and `tests.test_build_contract`: 5
  tests passed with workspace schema version 2 included in the process build
  contract.
- The Project foundation, DataVersion source-package, Project authoring, and
  build-contract run completed 42 of 43 tests. The remaining browser fixture
  stopped before an assertion because its generated Windows artifact path was
  273 units, above the portable 259-unit gate; no upgrade assertion failed.
- `scripts/documentation_quality.py --check`: passed.
- `tests.test_documentation_quality`: 5 tests passed.
- `git diff --check`: passed; Git reported only the repository's expected
  LF-to-CRLF notices.

## Semantic compatibility boundary

Storage migration preserves saved bytes and database facts; it does not grant
permission to reinterpret immutable evidence. A future Recipe, source-package,
snapshot, approval, or execution-payload contract change must retain an
explicit decoder for each supported old contract or publish a new immutable
successor. A schema-version bump cannot silently change the meaning of that
evidence.
