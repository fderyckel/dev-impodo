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

Before evaluation starts, Impodo binds the staging run to one project and the
exact physical and effective source selections. It also binds the submitted
mapping, governed schema, related-dataset plan, compiled-plan meaning,
evaluator and contract versions, declared control totals, and source-content
hashes.

Changed bindings produce a different run and retire the current pointer. They
never mutate historical staging evidence. Publication rechecks the bindings
inside the same transaction that advances the current pointer.

## Canonical rows and lineage

Every evaluated input produces a canonical result, including rows with blocking
issues. Each row records or retains the following evidence:

- The row records deterministic row and dataset coordinates.
- The row points to every physical source row that contributed to the result.
- The row identifies the target model, portable business identity, and scope.
- The row stores typed proposed scalar values and symbolic relationships.
- The row retains structured issues and field-level source lineage.

When a dataset has a `matching_rows` inclusion policy, staging evaluates that
policy before it prepares identities, target fields, relationships, impacts,
or control totals. An included row follows the normal canonical path. A
non-matching row becomes a lineage-only `EXCLUDED` decision with no proposed
Odoo values or symbolic relationships. A typed source value that cannot be
read becomes a lineage-only `BLOCKED` decision with a structured issue that
names the rule column. If the policy includes no rows, staging publishes a
dataset-level `ROW_INCLUSION_ZERO_INCLUDED` error.

A constant existing many2one remains a symbolic relationship. Its lineage
names the mapping rule and portable business-key value, but it does not claim
that a source column supplied that value. Repeated constants are deduplicated
before the later target-reference read plan.

A multi-column hierarchy evaluates two to five ordered source values with its
saved missing-value policies. Staging creates one generated row per distinct
complete path, creates every required ancestor, and stores the parent as a
same-dataset symbolic relationship. The consumer stores a symbolic reference
using its synthetic selected-path key. Preview and full staging call the same
path evaluator, so a fixed parent, promoted root, deepest populated level,
blank reference, quarantine, or block has one model-neutral meaning.

Portable staging recursively forbids numeric Odoo record IDs. Decimal, date,
datetime, null, and symbolic-reference values use canonical serialization.

## Disposition and accounting

Each canonical row has exactly one source-side disposition: `CANDIDATE`,
`REFERENCE`, `BLOCKED`, `QUARANTINED`, or `EXCLUDED`. These are not the later
target classifications `CREATE`, `UPDATE`, `UNCHANGED`, `AMBIGUOUS`, and
`BLOCKED`.

Reconciliation accounts for every canonical row and every physical source row.
Dataset controls record how many physical rows Impodo read and used, how many
canonical rows it produced, and how those rows are linked. They also record
grouping, derived fan-out, and source rows that produced no derived entity.
Excluded and row-inclusion-blocked decisions count as represented physical
rows, but they cannot enter business quality checks, control totals, Odoo
comparison, request planning, or execution eligibility.

## Determinism

Rows use deterministic dataset and source ordering, canonical JSON, and exact
bound evidence. Readers reject an unsupported version or malformed hash. They
also reject mismatched row lineage, duplicate row IDs, incomplete accounting,
inconsistent blocking status, numeric Odoo IDs, or a changed content hash.

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

Publication is immutable, atomic, project-scoped, and idempotent when the
current evidence is identical. The run header, rows, reconciliation,
current-pointer update, and audit event commit together. If any part fails,
Impodo rolls back the publication.

Preparation capability and row limits are selected from the actual compiled
mapping and snapshot path. Current measured limits and qualification evidence
belong in testing and reports, not in this contract.

## Related documentation

- [Prepare data implementation](../workflow/04-prepare-data.md)
- [Quality and quarantine contract](quality-and-quarantine.md)
- [Acceptance and test strategy](../../testing/acceptance.md)
