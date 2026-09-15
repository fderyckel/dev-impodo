# Qualify Match data ordering and Odoo refinement

## Status and remaining scope

**Status:** Structural ordering, persisted custom order, and the explicit live
Odoo check are implemented. Documentation reconciliation, visual acceptance,
and remaining performance qualification are open as of 2026-09-15.

The [Match data developer workflow](../developer/workflow/03-match-data.md)
owns the implemented recommendation algorithm, preference persistence, bounded
read plan, protected results, freshness rules, and single active attempt.
The [user workflow](../user/workflow/03-match-data.md) explains how to arrange
tables and apply a new suggestion. The original implementation phases remain
in Git history.

## Remaining work

1. Review the paired workflow pages, evidence lifecycle, Python code map, and
   workflow registrations against the current browser. Correct any remaining
   wording that presents the live Odoo refinement as unavailable.
2. Capture authenticated screenshots at 1440 by 1024 with fictional related
   tables. Show a recommendation explanation, a custom-order warning, partial
   live coverage, and the separate action that applies an updated suggestion.
3. Verify keyboard and pointer reordering, stale preference recovery, delayed
   checks, failure messages, and reopening an active check. A new recommendation
   must not silently move the active editor.
4. Record structural recommendation time for 1, 10, 100, and 1,000 datasets.
   Measure the live check at the current direct, relationship, and derived
   limits with repeated relationship keys. Retain raw timing, memory, and
   request-count evidence from isolated runs.
5. Run the focused ordering, persistence, browser, relationship, preflight,
   security, execution-regression, and documentation checks linked from the
   developer workflow.

## Acceptance boundaries

- Local rendering makes no hidden Odoo call. A custom queue changes only the
  workspace authoring preference, never mapping meaning or execution order.
- Live refinement uses the saved draft and current read credential. It merges
  and pages exact governed keys and reports incomplete coverage honestly.
- A stale draft, schema, target, or principal cannot acquire a current result.
  Failure preserves the previous completed result.
- Browser results contain aggregate reasons and counts. Exact values and Odoo
  identifiers remain behind the protected workspace evidence boundary.
- No source-row loop performs a repository query or connector call. Active
  checks keep navigation responsive and the work queue bounded.
- Final review still captures fresh target evidence. The advisory check cannot
  submit a mapping, authorize a writer, or satisfy load confirmation.

Remove this plan after the outstanding visual, documentation, and measured
qualification evidence is recorded in `docs/testing/` and current workflow
references have been verified.
