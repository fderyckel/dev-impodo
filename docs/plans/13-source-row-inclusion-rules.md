# Complete row inclusion across Recipes and Odoo transfers

## Status and remaining scope

**Status:** Ranked thirteenth in the cross-plan implementation order as of
2026-10-28. The 2026-10-31 review verified guided row inclusion, bounded
preparation, complete accounting, portable Recipe rules, and run review.
Applying those rules to Odoo-to-Odoo destination matching and loading remains
implementation work. Native columnar execution stays deferred unless
measurement makes the bounded Python fallback a release blocker.

The current rule and confirmation behavior are documented in the
[Match data workflow](../developer/workflow/03-match-data.md), and exclusion
accounting is documented in [Prepare data](../developer/workflow/04-prepare-data.md)
and the [canonical staging contract](../developer/contracts/canonical-staging.md).
Completed contract and browser delivery details remain in Git history.

The compiler currently returns `COLUMNAR_ROW_INCLUSION_UNSUPPORTED` and selects
the bounded Python evaluator for the entire dataset. Native execution must not
be advertised or selected for this rule until parity has been proved.

## Documentation and browser qualification

1. Reconcile the paired **Match data**, **Prepare data**, and **Final review**
   pages, quality contract, workflow registry, and Python code map against the
   implemented rule and its current fallback. Preserve their existing current
   explanations instead of copying the completed plan into them.
2. Capture authenticated decision and review states at 1440 by 1024 with
   fictional data. Show the original, included, excluded, and cannot-evaluate
   counts and the exact confirmation required for exclusions.
3. Verify keyboard access, invalid comparisons, zero matches, changed counts,
   and fresh Recipe applications with missing or ambiguous columns. Record
   browser and screenshot evidence under the testing workflow.

## Odoo 20 and remote Recipe qualification

Final Odoo `20.0` and `20.0+e` permit ordinary reads, loading, recovery, and
Recipe authoring. A Recipe must use the destination's major version. The
separate Odoo 20 Production promotion operation remains disabled. Remote
connections use HTTPS and JSON-2; row rules evaluate the accepted source
snapshot locally.

The 2026-10-31 fixture probe published an Odoo 20 rule, rebound it to fresh
columns, evaluated included and excluded rows, selected the Odoo 20 reference
policy, and rejected an Odoo 19 destination. It did not contact a live server.
Run a reproducible Recipe-reuse check against a disposable remote Odoo 20
database. Verify fresh counts, required confirmation, missing or ambiguous
columns, zero matches, invalid typed values, and exclusion from destination
writes. Retain only sanitized results and exact build information.

Reconcile the Odoo data workflow's stale claim that ordinary Odoo 20 writes
and recovery are disabled. Preserve the separate Production boundary.

## Odoo-to-Odoo implementation and qualification

The reviewed transfer path uses its own capture filters and explicit
put-aside decisions. It does not consume the Match data row-inclusion rule.
Connect the shared rule meaning and exact confirmed decisions to destination
matching, relationship checks, transfer review, preflight, loading, and
recovery. Keep explicit put-aside decisions independent of rule exclusions.

Require fresh confirmation when a rule excludes rows. An unknown column,
invalid comparison, cannot-evaluate result, or zero-match dataset must block
the transfer. Rule or source changes must invalidate dependent transfer
evidence. Every original row must remain accounted for, and an included
record whose required related record was excluded must remain blocked.

Qualify final Odoo 20 to final Odoo 20, including remote connections, repeated
checks, interrupted loading, and relationship dependencies. Preserve the
same-major gate and protected source identities. Protected same-target Odoo
updates remain ineligible for Recipe publication; this work does not make
numeric source identities portable.

## Deferred native execution follow-up

Activate this follow-up only after measurement justifies native execution.
Implement expressions only with parity against `source_condition_matches`
and `row_is_included`. Cover text, numbers, Boolean values, dates,
date-times, blanks, and values that cannot be evaluated.

The native route must preserve evaluation order: decide inclusion before
transforming target fields or relationships. Excluded or blocked rows retain
lineage and complete source accounting; only included rows enter comparison
and execution. A zero-match dataset remains blocked.

Verify identical ordered values, decisions, lineage, issues, and hashes across
batch sizes. Prove bounded memory and the absence of repository or Odoo access
inside source-row loops before changing the compiler capability. Existing row
limits remain in force until the complete route is qualified.

## Completion

The active work is complete when the transfer integration and remote Odoo 20
Recipe checks pass, and current documentation and authenticated captures agree
with the browser. Native parity is a separate deferred follow-up and is not
an acceptance condition for this delivery.
The default remains **Use every row** for old and new mappings. An inclusion
edit still invalidates dependent evidence, and Recipe publication stores only
portable rule meaning, without source values or observed counts.
