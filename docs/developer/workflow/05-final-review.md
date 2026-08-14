---
audience: developer
stage: review
status: current
---

# Final review

## Responsibility

Final review captures current target evidence, compares it with eligible
prepared rows, classifies every row, and publishes portable review artifacts
and the exact execution snapshot.

It is read-only with respect to Odoo and does not authorize a write.

## Entry conditions

Prepared, quality, resolution, and normalization evidence must be complete and
bound to the current source, schema, mapping, and compiled plan. The reader
must have the narrow scope derived from the compiled requirements.

## Implementation flow

`summary.py` renders the current local result. `preparation.py` can create the
prepared review input. `preflight.py` invokes `PreflightService.compare`, then
serves the manifest, workbook, and review package.

`PreflightService` freezes the input bindings, plans metadata and record
requests, captures the target fingerprint and snapshot, performs offline
classification, and publishes the report and execution snapshot atomically.

## Code references

| Role | Code |
| --- | --- |
| Comparison orchestration | [`PreflightService`](../../../src/impodo/application/preflight_service.py) |
| Frozen input | [`frozen_input.py`](../../../src/impodo/domain/preflight/frozen_input.py) |
| Review reports | [`reports.py`](../../../src/impodo/domain/preflight/reports.py) |
| Browser routes | [`preflight.py`](../../../src/impodo/web/routers/preflight.py) |

## Evidence and state

The target snapshot is target-specific and may contain protected Odoo IDs. The
portable report contains natural identities and deterministic classifications:
`CREATE`, `UPDATE`, `UNCHANGED`, `AMBIGUOUS`, and `BLOCKED`. The execution
snapshot binds only eligible writes to the exact reviewed evidence.

## Completion and navigation

Final review is complete only when the current report status is `READY`.
Ambiguous or blocked rows keep the stage in **Needs attention**. The load stage
remains locked until a ready report exists for the current bindings.

## Invalidation and recovery

Source, schema, mapping, compiled plan, prepared data, target fingerprint, or
dependency-order changes make the result stale. A transport HTTP status is not
the domain cause; retain the nested connector error and avoid automatic retries
when target state is uncertain.

Generated workbooks and packages are immutable outputs. Regenerate them from a
new comparison rather than editing their manifest.

## Odoo 19 and performance

The preflight planner groups metadata and record reads by target model. Keep
domains bounded and reject unrestricted record requests. Adding one
`search_read` per prepared row is an N+1 correctness and performance defect.

Target reads must use the narrow Odoo 19 read connector. No generic method call
and no write method belongs in this stage.

## Verification

- [`tests/test_preflight_service.py`](../../../tests/test_preflight_service.py)
- [`tests/test_preflight_scale.py`](../../../tests/test_preflight_scale.py)
- [`tests/test_engine.py`](../../../tests/test_engine.py)
- [`tests/test_connectors.py`](../../../tests/test_connectors.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)

Verify fixed classification precedence, batched requests, portable identities,
snapshot completeness, stale bindings, deterministic artifacts, and absence of
write capabilities.

## Related documentation

- [User guide: Final review](../../user/workflow/05-final-review.md)
- [Preflight contract](../contracts/preflight.md)
- [Quality and quarantine contract](../contracts/quality-and-quarantine.md)
- [Architecture decisions](../../decisions/README.md)
