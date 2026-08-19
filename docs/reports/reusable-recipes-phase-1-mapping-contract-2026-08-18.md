# Reusable recipes Phase 1 mapping-contract report — 2026-08-18

## Outcome

Phase 1 is complete. Impodo now has the strict current mapping and validation
boundary required before recipe extraction can be implemented. This phase does
not publish recipes or add series/data-version persistence.

## Implemented boundary

- Mapping contract v11 has four explicit categorical policies and strict nested
  parsing. Stored v8-v10 payloads retain their historical serialization and
  hashes.
- Scalar selections and relationship business keys require compatible explicit
  policy. Partial value matches cannot masquerade as exact passthrough meaning.
- One application-layer Polars scan reads all relevant columns of an affected
  physical dataset together. It uses immutable source snapshots, the same
  trimmed explicit-match semantics as runtime, and the runtime scalar evaluator
  for exact transformed target values.
- Validation contract v2 embeds bounded, hash-checked
  `CategoricalCoverageEvidence`; incomplete current domains are blocking and
  unsupported formula/reference providers remain visibly recipe-ineligible.
- Current target selection dependencies participate in evidence hashes.
  Relationship target existence and uniqueness are explicitly deferred to the
  existing fresh preparation check, so Phase 1 does not claim target-record
  coverage without record evidence.
- Reusable business-control definitions are separate from current-edition
  expected values. Runtime preparation consumes their exact paired projection.
  A full actor- and time-bound `EditionControlExpectation` contract is frozen
  for the later edition store.
- v8-v10 mappings receive a deterministic focused review/unsupported outcome;
  the browser requires a policy choice and never infers legacy intent.
- Browser source-choice enumeration now uses the shared bounded application
  service instead of a web-only rich-row reader.

## Qualification

Focused contract, workspace, validation, compiler, preparation-parity,
readiness, browser, and documentation tests pass. The final commands included:

```console
uv run --locked python -m unittest \
  tests.test_categorical_coverage \
  tests.test_mapping_validation \
  tests.test_columnar_compiler \
  tests.test_readiness.BrowserReadinessStagingTests.test_declared_business_total_uses_prepared_values_without_guessing \
  tests.test_preparation_scale.BoundedPreparationParityTests \
  tests.test_workspace \
  tests.test_documentation_quality
```

Result: 72 tests passed.

Changed Python files also pass Ruff, compile successfully, and the repository
diff has no whitespace errors. The full repository suite has one unrelated
existing browser assertion: the post-load summary intentionally renders
`Download review workbook`, while
`test_complete_project_setup_registration_without_yaml` also expects
`Recreate review workbook`. Neither that summary template nor assertion was
changed by Phase 1.

## Gate conclusion

The Phase 1 gate passes: a production v11 mapping check cannot omit categorical
evidence, incomplete explicit domains block submission, and legacy mappings do
not become recipe-eligible through inferred behavior. Recipe publication,
series/edition persistence, rebinding drafts, and the intent/outbox work remain
future phases. Per the authoritative roadmap, related/mixed 100,000-row
qualification resumes as the next priority.

**Subsequent priority decision, 2026-08-19:** Product ownership superseded the
project-series architecture, made Recipe-first Test-to-Production reuse the
only active product-delivery track, and deferred the related/mixed 100,000-row
qualification again. This does not change the Phase 1 evidence or implemented
mapping-v11 boundary.

## Related documentation

- [Implementation plan](../plans/reusable-recipes-and-data-versions-implementation-plan.md)
- [Frozen Phase 0 contracts](../plans/reusable-recipes-phase-0-contracts.md)
- [Workflow evidence lifecycle](../developer/contracts/evidence-lifecycle.md)
- [Impodo remaining work](../plans/remaining-work.md)
