# Recipe Phase R4 Test qualification implementation report

**Historical evidence:** This report records the superseded Recipe-first
implementation as it existed on 2026-08-19. ADR-014, current architecture, and
lifecycle contracts own behavior.

## Outcome

Phase R4 is complete. A data manager can qualify only the current immutable
Recipe revision after its exact remote Test application has completed the
existing preparation, quality, comparison, load, Odoo read-back, and
reconciliation flow. She must explicitly confirm the expected create, update,
unchanged, and verified totals before qualification, then separately select
that qualification as the rollout candidate.

Qualification does not contain an API key and grants no Production authority.
It protects the exact Recipe semantic hash, Test DataVersion and application,
TargetBinding, preparation and quality evidence, controls, comparison,
execution journal, read-back, reconciliation, and bounded expected outcomes.
The optional repeat-preview hash is represented explicitly and remains unset
when no repeat preview was requested.

## Current-revision rule

Qualification status is derived only for the Recipe's current revision. Prior
qualifications remain immutable history, but publishing a later revision makes
that new revision untested. A rollout candidate continues to identify the
previously selected exact revision and qualification; it is not silently
transferred to the new revision.

The registry also rejects a direct attempt to select a qualification that is
not for the current Recipe revision.

## UI integration

The existing six-stage workspace remains the preparation and execution UI.
Matching is unchanged. The Recipe overview now directs the user to the next
existing Test step, to qualification when the verified rehearsal is ready, or
to explicit rollout selection after qualification. The verified load result
links back to the focused Recipe qualification page.

## Verification

Focused coverage proves:

- exact successful Test evidence becomes ready for qualification;
- changed Test read-key generation blocks qualification;
- expected outcome confirmation is exact and optimistic;
- qualification and rollout-candidate selection are separate actions;
- Recipe v4 does not inherit Recipe v3 qualification;
- qualification payloads contain no numeric Odoo IDs or credentials;
- registry summaries expose qualification only for the current revision; and
- the focused browser surface retains the existing workspace stages.

The full repository suite passed after the implementation.
