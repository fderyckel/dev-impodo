# Qualify hierarchy-root loading and group dependency causes

## Status and remaining scope

**Status:** The hierarchy-root semantic correction, bounded preflight lookup,
Recipe reuse, and frozen execution-order tests are implemented. Live local and
remote Odoo 19 qualification and grouped dependency explanations remain open
as of 2026-09-15.

Current behavior belongs to the [Match data developer workflow](../developer/workflow/03-match-data.md),
[Prepare data](../developer/workflow/04-prepare-data.md), and
[Load into Odoo](../developer/workflow/06-load-into-odoo.md#current-relationship-ordering).
The [related-tables guide](../user/guides/related-tables.md) explains hierarchy
authoring. Completed design alternatives and incident evidence remain in Git
history.

Generated roots use `explicit_scope_null` for their optional self-parent scope.
Populated parent keys remain dependencies, while ordinary target keys and
partially blank composite keys remain required. Relational identity operations
still select whole-dataset Python fallback. Historical version-16 mappings
retain their original `reject` meaning.

## Live acceptance still required

Use isolated fictional hierarchies on disposable local and remote Odoo 19
targets. This plan records the qualification requirement; it does not authorize
an affected user's Production load.

- Cover new and existing roots, children, repeated leaf names under different
  parents, duplicate roots, and records that consume the generated hierarchy.
- Verify exact root matching with an unset parent and exact descendant matching
  through the complete lineage. Keep target requests bounded by the existing
  preflight plan, with at most 500 lineage expressions in a request group.
- Verify parent-before-child execution and zero relationship-completion writes
  for acyclic hierarchies. A child must use the journalled local identifier or
  remote External ID of the exact parent.
- Exercise required cycles, missing or ambiguous parents, known rejection,
  unknown outcomes, interruption, and exact read-back before retry. No child
  can be released without its durable parent receipt.
- Reconcile the same logical hierarchy on both transports and record request
  counts, hashes, and retained outcomes at the current supported row limits.

## Group propagated dependency consequences

Final review should show each actionable root cause once and group the rows
blocked by that cause beneath it. Distinguish a missing, ambiguous, excluded,
or quarantined parent from its affected descendants and consuming records.
Preserve complete accounting and the existing fail-closed eligibility rules.

Verify that a data manager can find the owning correction action without
acknowledging thousands of repeated consequences. Add focused presenter and
browser checks and capture the authenticated review state after implementation.

## Recovery boundary

An affected workspace must preserve its failed preparation as history and
create a new mapping revision with the explicit root-capable scope. Require
**Check matches**, preparation, and **Check changes** again before loading.
Keep accepted source data and earlier approvals unchanged; an earlier warning
acknowledgement cannot authorize the new plan.

Remove this plan after live acceptance and grouped-cause presentation have
passed and the paired workflow and testing documentation records the result.
