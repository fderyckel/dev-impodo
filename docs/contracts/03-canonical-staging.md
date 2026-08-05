# Canonical staging evaluation contract

## Status and boundary

**Status:** Implemented as a durable, target-independent foundation. Canonical
runs and typed rows are atomically published in each project's DuckDB database.
The separate quality overlay now implements quarantine; normalization approval,
package certification, and Odoo execution are not implemented by this contract.

The browser evaluator applies one exact submitted mapping and derived-entity
plan to every frozen source row. It produces deterministic canonical evidence
without reading or changing Odoo:

```text
frozen source tables within the supported browser limit
-> storage-independent full-row evaluator
-> typed prepared records
-> versioned canonical staging run
-> atomic project-scoped DuckDB publication
-> existing read-only preflight compatibility path
```

Artifact materialization remains an adapter responsibility. The evaluator
accepts already loaded physical tables and has no repository, connector,
credential, or Odoo dependency.

The current browser path accepts at most **25,000 physical source rows per
project**. It checks that limit before materializing an artifact and gives the
data manager a plain instruction to split a larger source. This is an explicit
interim boundary, not a claim that source evaluation is streaming.

## Bound inputs

Every `CanonicalStagingRun` binds:

- project and mapping identifiers;
- physical and effective source-selection hashes;
- submitted mapping and governed schema hashes;
- the derived-entity plan hash when one exists;
- staging-contract and evaluator versions;
- any explicitly declared business totals through the mapping hash;
- exact source-content hashes through each row's lineage.

Changed bound inputs produce different canonical row and run hashes. Publishing
rechecks those inputs inside the database transaction. Source, related-entity,
schema, mapping, or target changes retire the current pointer without deleting
historical canonical evidence.

## Canonical rows and lineage

Every evaluated row produces one `CanonicalRow`, including rows with blocking
issues. It retains:

- a deterministic row identifier;
- dataset coordinates plus every contributing physical source-row pointer;
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
Per-dataset controls also record physical rows read and used, canonical rows
produced, lineage links, grouped source rows, additional derived rows, and
source rows that did not create a derived entity. Direct, lookup, parent, and
child transformations therefore remain distinguishable. Canonical evaluation
does not rewrite these base dispositions. The separate quality overlay computes
the effective quarantined or excluded state and filters ineligible rows before
Odoo request planning.

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
Readiness reports bind the exact published staging run and content hash.

Server-rendered mapping previews and full-row evaluation both call
`evaluate_scalar_mapping_value()`. The browser preview is therefore a bounded
projection of the same provider, formula-context, transformation, parsing, and
validation semantics used for every row. Client-side instant feedback remains
advisory until the mapping is saved and checked by the server.

## Explicit business control totals

Business totals are opt-in mapping evidence. For each configured check the
data manager must provide a plain name, choose a mapped numeric Odoo field,
enter the expected total, and state the currency or unit when relevant. The
only supported aggregation is `SUM`; Impodo never guesses an amount field,
quantity field, currency, unit, or expected value.

Evaluation adds the typed canonical values, counts included and empty rows,
compares the result with the declared tolerance, and persists the deterministic
result with the canonical run. Empty values prevent a total from passing. A
failed total prevents review-package creation but does not modify source data
or grant any Odoo capability. The Review page shows the business result openly
and keeps field identifiers and tolerance details collapsed.

## Publication lifecycle

Canonical evidence is immutable. Publication time, operator, and lifecycle
status are stored separately from its deterministic content hash. A successful
publication inserts the run header and rows in bounded batches, verifies the
stored row count, supersedes the previous current run, updates the current
pointer, and writes one audit event in the same transaction. Failure rolls the
whole publication back. Re-publishing identical current evidence is
idempotent.

The Review page exposes only a plain-language saved status and row total.
Dataset controls, run identifiers, versions, and hashes remain inside collapsed
technical details. Odoo is not contacted by the staging repository.

## Current scale evidence and quality integration

On 2026-08-05, the earlier evaluator-only probe processed a synthetic
100,000-row fixture, but it excluded the durable quality overlay and therefore
does not define the product limit. The completed integrated probe processed
25,000 physical rows into 25,001 canonical and quality rows in 45.392 seconds,
with 348.0 MiB peak RSS and a 66.3 MiB project database on the development
Windows workstation. The lower completed integrated bound wins. Wider sources
and streaming beyond it remain later scale work.

Governed quality rules, physical-source accounting, and quarantine are defined
in the [quality and quarantine contract](06-quality-and-quarantine.md). Durable
staging and quality evidence are not a clean package or Odoo write
authorization.

## Executable evidence

- [`staging_contracts.py`](../../src/impodo/staging_contracts.py)
- [`staging.py`](../../src/impodo/staging.py)
- [`evaluator.py`](../../src/impodo/domain/staging/evaluator.py)
- [`control_totals.py`](../../src/impodo/domain/staging/control_totals.py)
- [`preparation_service.py`](../../src/impodo/application/preparation_service.py)
- [`staging_repository.py`](../../src/impodo/adapters/duckdb/staging_repository.py)
- [`test_readiness.py`](../../tests/test_readiness.py)
- [`test_staging_store.py`](../../tests/test_staging_store.py)
