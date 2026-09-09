---
audience: developer
kind: report
status: current
---

# Recipe workflow phase 3: constrained target review

This phase gives developers the behavior, ownership, and evidence needed to
review the target-value changes following
[delivery control expectations](recipe-workflow-phase-2-2026-09-09.md).
It closes the focused review's misleading return to field authoring while
preserving the existing mapping contract.

## Resulting behavior

The run review verifies each complete composite or scoped Many2one key against
the supporting values captured during Check Odoo. It retains component order
and company scope. Exact matches are read-only. Missing and ambiguous keys
remain blocked and explain whether the operator must correct the delivery,
correct Odoo, or publish a new Recipe version.

The existing contract permits explicit translations only for single-key,
scope-free relationships. This phase does not extend that contract. Opening
the full field matcher could not make a composite translation valid, so the
focused review no longer offers that action. Validation failures also remain
on the focused page. Unsupported source domains and uncovered fixed providers
cannot be presented as a successful target check.

Selection and permitted Many2one decisions still pass through the canonical
mapping validation and submission services. Accepted control totals and Recipe
meaning stay fixed. Ambiguous target keys cannot be selected or suggested;
duplicate display labels do not produce a suggested match.

## Ownership and concurrency

[RecipeTargetMatchService](../../src/impodo/application/run/target_matches.py)
now owns review and confirmation. The previous web-owned module is removed.
The route validates the session and form envelope, delegates the command to
the thread pool, and renders the outcome. Source scans and confirmation work
therefore do not execute directly on the async route's event loop.

The service derives permitted decision names from current evidence. Before
writing it verifies the working-draft version, mapping hash, and a review hash
covering source coverage, schemas, read identity, and lookup contents. Stale
forms are rejected without replaying their answers onto a changed review.
The compiled Recipe baseline is checked before saving, and the existing
submission policy remains authoritative. Applications that have progressed
beyond target review cannot resubmit these decisions.

Capture and review share the existing portable tuple serialization in
[supporting_lookups.py](../../src/impodo/domain/workspace/supporting_lookups.py).
The serialized format is unchanged, so this phase requires no storage upgrade.
The reviewed production inventory remains at 420 modules, with 2,443 runtime
import edges, no cycles, and no application-to-adapter imports.

## Efficiency evidence

Each review collects source coverage once and reads each distinct supporting
lookup once. Suggestion construction now indexes unique labels and values
once per field instead of searching every target choice for every source
value. Its work grows with the sum of source and target choices instead of
their product. Source scanning and canonical validation retain their existing
behavior.

A local Python 3.14 microbenchmark compared the previous `_suggested_choice`
implementation from commit `4791d08` with the new index, including index
construction. It used 1,000 source labels, 1,000 target choices, and seven
repetitions. Outputs were identical. Median time was **81.286 ms before** and
**0.686 ms after**. This measurement covers suggestion construction and lookup
only; it excludes source scans, database work, Odoo requests, and rendering.
No overall workflow speedup or memory reduction is established by this result.

## Verification

The 54 focused regression checks passed across target decisions, categorical
coverage, supporting lookups, target readers, Recipe mapping protection, and
architecture. Another seven checks passed for run recovery routing, the
existing Fresh data journey through preparation, and documentation.

The authenticated target-review journey also passed with the real compiler,
mapping validator, submission service, and preparation service. It rejects
invalid, forged, and stale decisions before saving. It also verifies that stale
answers are discarded when the page is rendered again. It confirms the valid language
translation, advances the application to Ready, and evaluates the accepted
125.50 total successfully during preparation. Its Odoo metadata is fictional;
it makes no external Odoo call or write.

Focused evidence lives in:

- [Service tests](../../tests/application/run/test_target_match_service.py),
  which exercise complete keys, company separation, ambiguity, stale evidence,
  unsupported providers, fixed Recipe rules, and lookup call counts.
- [Decision tests](../../tests/application/run/test_recipe_target_matches.py),
  which retain verified values while adding permitted translations.
- [Authenticated journey](../../tests/integration/web/test_recipe_target_matches.py),
  which connects Fresh data, compilation, target review, and preparation.

The [current target-review screenshot](../images/user/03b-recipe-target-values.png)
was captured in authenticated headless Edge at 1440 by 1024 and inspected.
The [capture helper](../../scripts/capture_recipe_target_matches.py) reproduces
the decision point with isolated fictional data. Documentation and diff checks
passed. These runs do not constitute the full repository suite.

## Remaining boundaries

Other mapping blockers still use their existing general mapping recovery path.
Composite and scoped key translation would require an explicit domain and
execution-contract change, with coverage through preparation and target
resolution; adding a selector alone is insufficient.

At this phase's completion, durable recovery from lost final worker
notifications and remaining preparation enqueue orchestration were open.
[Phase 4](recipe-workflow-phase-4-2026-09-09.md) subsequently added recovery and
separated commands from the run view. Production setup alignment and representative
qualification-to-Production acceptance remain open. Whole-workflow throughput
and peak-memory measurements also remain open. The previously reported
test-organization failure in the unchanged 2,096-line mapping workflow test
was not part of this phase's targeted runs.
