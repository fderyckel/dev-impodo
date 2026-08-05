# Quality and quarantine contract

## Status and boundary

**Status:** Implemented for the bounded browser workflow.

Impodo evaluates deterministic data checks after canonical staging and before
read-only Odoo comparison. The quality layer never edits registered source
files or canonical staging rows. It publishes an immutable overlay and sends
only eligible rows into Odoo preflight:

```text
current canonical staging
-> current versioned data checks
-> immutable quality run
-> Ready / Review / Set aside / Fix setup
-> eligible rows only
-> batched read-only Odoo comparison
```

This is not clean-package certification, execution approval, or Odoo write
authorization. Those remain later gates.

## Rules and outcomes

Mandatory automatic checks cover required and bounded scalar values, governed
lookups, relationship readiness, and post-transformation identity collisions.
The data manager can add at most three guided cross-field checks per dataset:
required-if, exactly-one-of, ordered comparison, equality, or inequality.

Rules are versioned, deterministically ordered, and bound to the exact mapping
and schema. Their outcomes are:

- `WARNING`: keep the row eligible and ask for review;
- `BLOCK`: stop the project because setup or policy is incomplete;
- `QUARANTINE`: set the affected business record aside;
- `EXCLUDE`: omit only under an explicit governed rule. The normal browser UI
  does not offer this advanced outcome.

Unknown values, ambiguous matches, parse failures, and unsupported contexts
never become silent exclusions. Identity collisions set aside the complete
collision group. A relationship to a set-aside incoming record propagates the
safe outcome to the dependent row without an Odoo call.

## Complete accounting

Every canonical row has one `QualityRowResult`. Every physical source row has
one `SourceAccountingEntry`, with fan-out links to all canonical records it
contributed to. This deliberately permits mixed canonical outcomes when one
physical row creates several business records.

`UNREPRESENTED` physical rows, missing mandatory rule families, stale rule
fields, incomplete evidence, and inconsistent hashes fail closed. Set-aside
and governed-excluded rows are removed before Odoo request planning, while
their evidence stays visible and reconciled.

## Persistence and lifecycle

Quality rules, runs, row results, issues, accounting links, and quarantine
entries are stored in the project DuckDB database. Publication is atomic and
idempotent. A failed publication keeps the prior current run. A successful
rerun supersedes the previous current run and retains its history.

Each quality run binds the canonical staging content hash, ruleset hash,
mapping hash, schema hash, evaluator version, and project ownership/retention
context. A change to any bound input invalidates the current quality result.
Review dates cannot exceed project retention. Evidence records identifiers and
plain correction routes, not raw field values or numeric Odoo record IDs.

## Data-manager interface

The normal interface uses four states: **Ready for Odoo check**, **Review**,
**Set aside**, and **Fix setup**. A result row shows its table, source row,
business label, finding, owner, and next action. Technical identifiers, hashes,
and evaluator details are collapsed.

Automatic checks are read-only recommendations in a collapsed **Data checks**
section. Optional checks use labelled fields and business-language outcomes;
the data manager never enters JSON, Python, SQL, Odoo domains, field IDs, or
model names.

## Bounded execution and Odoo access

The materializing browser workflow accepts at most **25,000 physical source
rows per project**. On the development Windows workstation, the integrated
25,000-row parent/child fixture produced 25,001 canonical and quality rows in
45.392 seconds, used 348.0 MiB peak RSS, and stored a 66.3 MiB project database.
The 10,000-row probe completed in 19.464 seconds at 179.7 MiB peak RSS. These
are workstation acceptance measurements, not production sizing guarantees.

DuckDB evidence writes use bounded bulk JSON relations. Quality evaluation
uses in-memory grouped indexes and makes no Odoo request. Eligible Odoo reads
remain planned by model and paged; there is no connector or database query per
source row.

## Executable evidence

- [`quality.py`](../../src/impodo/quality.py)
- [`readiness.py`](../../src/impodo/readiness.py)
- [`project_store.py`](../../src/impodo/project_store.py)
- [`app.py`](../../src/impodo/web/app.py)
- [`test_quality.py`](../../tests/test_quality.py)
- [`test_staging_store.py`](../../tests/test_staging_store.py)
- [`test_readiness.py`](../../tests/test_readiness.py)
- [`test_web_app.py`](../../tests/test_web_app.py)
