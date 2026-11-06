---
audience: developer
kind: plan
status: proposed
---

# Morrow Assembly practice pack

These fictional files are the draft dataset for the
[shared practice-data proposal](../24-shared-practice-data-and-scenarios.md).
They let reviewers inspect the proposed business story, source bytes, expected
results, and deliberately introduced faults before deciding whether to promote
them into maintained learner exercises or executable scenarios.

## Review the draft

1. Read the proposal for the intended audience, workflow, and acceptance checks.
2. Use [the manifest](manifest.json) to identify the supplied deliveries and
   their recorded identities. Inspect [the data dictionary](data-dictionary.json)
   before interpreting column names or identifiers.
3. Read [the exercise contracts](exercise-contracts.json) and
   [expected business results](expected-business-results.json) together. Physical
   source-row totals and the expected prepared or loaded record totals have
   different meanings.
4. Review [the target seed projection](target-seed-projection.json) and the
   [vendor-term repair overlay](target-overlays/vendor-term-repair.json) before
   designing a disposable target fixture. They describe intended business
   records and do not authorize changes to a live Odoo database.
5. Inspect [the recorded validation evidence](validation-evidence.json) and the
   [scenario catalogue](../24-practice-scenario-catalogue.md). They identify the
   checks and qualification still needed before promotion.

## File ownership

Baseline CSV inputs are under `deliveries/2026-04-15/`. A replacement delivery
is under `deliveries/2026-05-15/`, and a narrow customer update is under
`patches/2026-05-20/`. Keep those deliveries distinct when reviewing Recipe
reuse or update-only behavior.

The JSON files under `faults/` describe separate fault variations. Start from
the specified clean delivery for each variation, apply only its stated change,
and retain its expected blocker or recovery outcome. Combining variations
requires an explicit scenario contract.

This pack remains proposed material. Its presence does not establish a tested
Recipe, supported scale limit, qualified Odoo target, or production-ready
migration. Existing test fixtures and scenarios retain their current owners.
