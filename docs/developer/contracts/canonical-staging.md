---
audience: developer
kind: contract
status: current
---

# Canonical staging contract

## Scope

Canonical staging applies one exact submitted mapping and related-dataset plan
to every frozen source row. It publishes deterministic, target-independent
prepared evidence and makes no Odoo request.

Artifact materialization is an adapter responsibility. The evaluator accepts
loaded source tables and has no repository, connector, credential, or Odoo
dependency.

## Bound inputs

Every staging run binds the project, physical and effective source selections,
submitted mapping, governed schema, related-dataset plan, compiled-plan
semantics, evaluator/contract versions, declared control totals, and exact
source-content hashes.

Changed bindings produce a different run and retire the current pointer. They
never mutate historical staging evidence. Publication rechecks the bindings
inside the same transaction that advances the current pointer.

## Canonical rows and lineage

Every evaluated input produces a canonical result, including rows with blocking
issues. Each row retains:

- deterministic row and dataset coordinates;
- all contributing physical source-row pointers;
- target model, portable business identity, and scope;
- typed proposed scalar values and symbolic relationships;
- structured issues and field-level source lineage.

Portable staging recursively forbids numeric Odoo record IDs. Decimal, date,
datetime, null, and symbolic-reference values use canonical serialization.

## Disposition and accounting

Each canonical row has exactly one source-side disposition: `CANDIDATE`,
`REFERENCE`, `BLOCKED`, `QUARANTINED`, or `EXCLUDED`. These are not the later
target classifications `CREATE`, `UPDATE`, `UNCHANGED`, `AMBIGUOUS`, and
`BLOCKED`.

Reconciliation requires every canonical row and every physical source row to
be accounted for. Dataset controls retain physical rows read and used,
canonical rows produced, lineage links, grouping, derived fan-out, and source
rows that did not produce a derived entity.

## Determinism

Rows use deterministic dataset/source ordering, canonical JSON, and exact bound
evidence. Readers reject unsupported versions, malformed hashes, row/lineage
mismatches, duplicate row IDs, incomplete accounting, inconsistent blocking
status, numeric Odoo IDs, or a changed content hash.

Mapping preview and full evaluation must share the same scalar-provider,
formula, transformation, parsing, and validation semantics. Client-side
feedback remains advisory until the server publishes checked evidence.

## Control totals

Business totals are opt-in mapping evidence. The data manager explicitly names
the check, selects a mapped numeric field, supplies the expected total and
currency/unit, and may define a tolerance. Impodo never guesses any of these.

Only `SUM` is supported. Empty values prevent a passing result. A failed total
blocks review-package creation but does not change source evidence or grant an
Odoo capability.

## Publication

Publication is immutable, atomic, idempotent for identical current evidence,
and project scoped. Run header, rows, reconciliation, current-pointer update,
and audit event commit together; failure rolls the publication back.

Preparation capability and row limits are selected from the actual compiled
mapping and snapshot path. Current measured limits and qualification evidence
belong in testing and reports, not in this contract.

## Related documentation

- [Prepare data implementation](../workflow/04-prepare-data.md)
- [Quality and quarantine contract](quality-and-quarantine.md)
- [Acceptance and test strategy](../../testing/acceptance.md)
