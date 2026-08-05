# Canonical staging evaluation contract

## Status and boundary

**Status:** Implemented as an in-memory, target-independent foundation. Durable
DuckDB publication, quarantine workflow, normalization approval, package
certification, and Odoo execution are not implemented by this contract.

The browser evaluator applies one exact submitted mapping and derived-entity
plan to every frozen source row. It produces deterministic canonical evidence
without reading or changing Odoo:

```text
materialized frozen source tables
-> storage-independent full-row evaluator
-> typed prepared records
-> versioned canonical staging run
-> existing read-only preflight compatibility path
```

Artifact materialization remains an adapter responsibility. The evaluator
accepts already loaded physical tables and has no repository, connector,
credential, or Odoo dependency.

## Bound inputs

Every `CanonicalStagingRun` binds:

- project and mapping identifiers;
- physical and effective source-selection hashes;
- submitted mapping and governed schema hashes;
- the derived-entity plan hash when one exists;
- staging-contract and evaluator versions;
- exact source-content hashes through each row's lineage.

Changed bound inputs produce different canonical row and run hashes. This
contract does not decide approval invalidation or repository publication; those
rules belong to the durable staging slice.

## Canonical rows and lineage

Every evaluated row produces one `CanonicalRow`, including rows with blocking
issues. It retains:

- a deterministic row identifier;
- dataset and physical source-row coordinates;
- source identity, target model, target identity, and scope;
- typed proposed scalar values;
- unresolved symbolic relationships expressed through business keys;
- structured issues;
- source, mapping, schema, derived-plan, and field-source lineage.

Numeric Odoo record IDs are forbidden recursively from the portable run.
Decimal, date, datetime, null, and symbolic-reference values use the existing
canonical serialization rules.

## Source-side disposition and reconciliation

Each canonical row has exactly one source-side disposition:

| Disposition | Current meaning |
| --- | --- |
| `CANDIDATE` | A create/upsert row has no blocking source-side issue |
| `REFERENCE` | A reference-only row has no blocking source-side issue |
| `BLOCKED` | One or more error-severity source-side issues prevent safe continuation |
| `QUARANTINED` | Reserved for the integrated quarantine workflow |
| `EXCLUDED` | Reserved for a governed exclusion workflow |

`StagingReconciliation` requires the candidate, reference, blocked,
quarantined, and excluded counts to equal the total canonical row count.
Current evaluation never labels a row quarantined or excluded because those
governed workflows are not yet integrated.

These dispositions are not Odoo preflight classifications. Target-dependent
`CREATE`, `UPDATE`, `UNCHANGED`, `AMBIGUOUS`, and `BLOCKED` remain the output of
the read-only preflight engine.

## Determinism and compatibility

Canonical rows use deterministic dataset/source-row ordering. Row IDs and the
run content hash use canonical JSON and exact bound evidence. The reader rejects
unsupported versions, malformed hashes, row/lineage mismatches, duplicate row
IDs, incomplete reconciliation, blocking-status mismatches, numeric Odoo IDs,
or a changed content hash.

`stage_browser_mapping()` remains the compatibility adapter used by the
browser. It materializes frozen artifacts and delegates evaluation to
`evaluate_browser_mapping()`. The existing readiness planner and preflight
engine continue to consume the same `ProfileDocument` and `PreparedBundle`.

## Next integration slice

Durable staging must atomically publish the canonical run and typed rows in the
project DuckDB, record invalidation and supersession, preserve bounded batch
behavior, and expose reconciliation evidence without turning it into a clean
package or Odoo write authorization.

## Executable evidence

- [`staging_contracts.py`](../../src/impodo/staging_contracts.py)
- [`readiness.py`](../../src/impodo/readiness.py)
- [`test_readiness.py`](../../tests/test_readiness.py)
