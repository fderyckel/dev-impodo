# Complete row-inclusion documentation and native parity

## Status and remaining scope

**Status:** Guided row inclusion, bounded preparation, complete accounting,
Recipe reuse, and run review are implemented. Visual qualification and native
columnar parity remain open as of 2026-09-15.

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

## Native execution follow-up

Implement native row-inclusion expressions only with parity against
`evaluate_source_condition`. Cover text, numbers, Boolean values, dates,
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

The open work is complete when current documentation and authenticated captures
agree with the browser and native parity passes the appropriate scale gate.
The default remains **Use every row** for old and new mappings. An inclusion
edit still invalidates dependent evidence, and Recipe publication stores only
portable rule meaning, without source values or observed counts.
