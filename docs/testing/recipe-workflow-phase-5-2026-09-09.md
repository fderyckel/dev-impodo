---
audience: developer
kind: report
status: current
---

# Recipe workflow phase 5: Production readiness and recovery

This report gives developers the behavior and evidence needed to review
Production activation after [phase 4](recipe-workflow-phase-4-2026-09-09.md).
Production now shares Test's typed value prompts and Recipe validators.
Setup completion follows the saved activation operation through compilation,
and an interrupted setup can finish from its original inputs after restart.

## Changes and ownership

`ProductionRunValuesUseCase` reads the qualified Recipe revisions in one
batch, checks their semantic hashes, and builds shared parameter prompts and
Recipe-specific control totals. The application normalizes direct API calls
as well as browser submissions. Export date comes from the new delivery,
invariant expectations remain fixed, and required totals must be finite.
Stale browser forms and invalid answers fail before the write probe or vault
update. Form errors retain editable answers without redisplaying the key.

The activation intent now retains versioned canonical answers and the observed
non-secret write identity. It already owns the target schema, required
references, credential generations, operation identity, and expected revision.
Resume revalidates current plan and qualification meaning, then continues that
reserved operation before or after registry commit. It keeps the original
reference provenance even when the initial capture contained unused lists.
It skips application mappings already completed and does not probe Odoo or
restore session credentials. Execution still requires fresh access checks.

An active Production binding can exist before compilation finishes. The
readiness page, run entry, preparation command, and execution authority guard
now require the activation operation's final commit. Project overview
distinguishes setup completion from a completed Production run. Production
run pages read their own plan binding, and their Check Odoo links return to
Production readiness. The readiness sidebar keeps the user in Check Odoo
until setup finishes.

Older pending activations without reconstructable answers retain their history
and direct the operator to fresh setup. Existing completed activations remain
readable. There is no schema migration or new recovery database.

## Efficiency and architecture

Shared templates and canonical validators replace Production's generic text
fields and duplicate form meaning. Recipe metadata reads are batched. Project
overview obtains all setup completion states in one additional registry query
that excludes intent payloads. Resume avoids redoing completed compiler work;
it does not copy Test source rows or result evidence.

Activation and readiness rendering run in the thread pool. This keeps
compilation off the async event loop but does not turn activation into a
background job. Selected-run recovery checks still read the saved intent;
large reference payloads warrant a dedicated status projection if profiling
shows material cost. No whole-workflow speed or peak-memory improvement is
claimed without representative measurements.

The current inventory contains 424 production modules and 2,475 runtime import
edges. Dependency checks report no cycles or application-to-adapter imports.

## Verification

Across the completed focused runs, 64 distinct checks passed. Documentation,
architecture inventory, and diff checks also passed. The reference-recovery
fixture initially reused a stale Project revision when creating its second
Test run; it now reads the current revision, and the corrected test passes.

- [Production service tests](../../tests/application/run/test_production_rollout.py)
  cover separate Production identity and authority, fresh source ownership,
  actor-bound resume before and after registry commit, competing operations,
  changed schema, and filtered reference recovery.
- [Typed value tests](../../tests/application/run/test_production_values.py)
  cover shared numeric parameters, automatic dates, required and fixed totals,
  nonfinite input, stale form evidence, and retained editable answers.
- [Authenticated Production journey](../../tests/integration/web/test_production_readiness.py)
  publishes and selects a real qualified plan, then accepts a separate
  Production delivery. Test totals 125.50 EUR; Production totals 200.00 EUR.
  It compiles actual mappings, interrupts final activation commit, restarts
  with artifact encryption keys retained and session Odoo keys cleared, and
  resumes without another write probe or compiler materialization. Actual
  preparation verifies the Production total. It also checks navigation and
  that unfinished setup cannot prepare or load.
- Existing Fresh data, preparation recovery, identity, journey, ownership, and
  documentation checks cover the shared components and boundaries.

The browser fixture substitutes Odoo metadata, the write probe, and completed
Test execution evidence. Qualification publication/authentication, source
acceptance, Recipe compilation, and Production preparation use the actual
application. This is not live Odoo qualification-to-load acceptance.

The [readiness](../images/user/05a-production-readiness.png) and
[resume](../images/user/05b-production-resume.png) screenshots show isolated
fictional data in the authenticated current UI. The
[capture helper](../../scripts/capture_production_readiness.py) uses headless
Edge at 1440 by 1024. It performs no external Odoo call or write.
Both images were visually inspected after capture.

The full repository suite and live Odoo acceptance were not run. Previously
reported preparation-message and mapping-test-size failures remain outside
this phase; this report does not claim they have been fixed.

## Remaining work

Production still uses generic source confirmation and Odoo capture pages.
Its full Fresh data upload and logical table-matching journey has not yet
converged with the guided Test flow. Unsubmitted Production answers are not
persisted as a draft. The next acceptance boundary is a representative
qualified Test through Production comparison, controlled load, and verified
read-back, followed by timing and peak-memory measurements at realistic sizes.
