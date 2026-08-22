# Migration Projects Phase M5 integrated qualification

## Status

**Status:** Implemented on 2026-08-22.

This implementation record sits under
[ADR-014](../decisions/README.md#adr-014--migration-projects-coordinate-reusable-recipes-and-cutover-plans).
The [Cutover plan lifecycle contract](../developer/contracts/cutover-plan-lifecycle.md)
is the normative current behavior.

## Delivered boundary

M5 adds immutable Project Cutover plan revisions, exact per-application and
integrated Test qualification, a dependency write guard, and a separate
rollout-candidate selection. It uses the existing application workspaces and
six-stage execution evidence. It does not add Production data intake or grant
Production authority.

## Gate evidence

The focused M5 suite proves:

- exact Test evidence qualifies all applications and one complete plan;
- selection is separate and leaves Production authority empty;
- a changed dependency appends a new unqualified plan revision;
- protected evidence publication recovers after a cross-store fault;
- a downstream application cannot write before its predecessor is verified;
- the browser explains incomplete evidence and the Production boundary; and
- the existing M4 planning path creates the plan binding without restoring
  Recipe ownership.

Run:

```console
python -m unittest tests.test_migration_project_phase_m5_cutover_qualification -v
```

## Storage decision

The registry generation is `impodo-migration-registry-2026-08-m5`. Earlier
development generations are rejected rather than upgraded. Qualification
payloads use a separate Project-scoped encrypted store so Recipe revision
keys, target-specific Test evidence, and bounded registry projections remain
separate concerns.

## Next boundary

M6 must accept a fresh complete Production DataVersion and create a fresh
Production run with independent target, credential, comparison, approval,
execution, and reconciliation evidence. M5 selection alone cannot satisfy any
of those checks.
