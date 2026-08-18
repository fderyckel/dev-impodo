# Reusable recipes Phase 0 baseline — 2026-08-18

## Purpose

This report records the current behavior that later reusable-recipe phases must
preserve or change explicitly. Recipe, series, and data-version runtime behavior
is not implemented at this baseline.

**Repository revision:** `f421dfd0cca50597d8c297a6a493d73a30995cc3` plus
the uncommitted Phase 0 documentation/fixture slice.  
**Python:** 3.12.13 from `.venv`.

## Current baseline

### Project list and lifecycle

`ProjectRepository.list` reads only `project_registry` and returns project ID,
name, status, revision, and update time. It does not open contained project
databases. Registry synchronization already has a pending receipt and
out-of-order-write protection, but there is no series, edition, pending-edition,
recipe, or history projection.

The implemented project transition remains `DRAFT -> REGISTERED`. Source files
may change only before source selection is frozen. Project deletion governs one
contained workspace and its project-scoped credentials.

### Mapping submission

`MappingWorkspaceService.submit_current` requires the exact current checked
revision, working-draft revision, source selection, live Odoo schema, and schema
governance hash. Submission does not rewrite the mapping. Mapping contract v10
is current and versions 8-10 remain readable. `MappingDefinition` retains
physical dataset/column identities plus exact source-selection/schema hashes;
it is not a reusable recipe.

### Source value choices

`impodo.web.target_readers._source_value_choices` verifies one frozen physical
snapshot and counts one requested source column. It is a web helper and is
called per field. Existing explicit value mappings are portable strings and
selection validation checks mapped target values, but there is no immutable
multi-field categorical coverage evidence and an unmapped scalar can still use
the current fallback semantics.

### Preparation

`PreparationService.prepare` verifies the current mapping submission, source
selection/snapshots, Odoo-origin protected provenance where applicable,
derived plan, quality ruleset, approved coverage, and reference bundle before
compiling capability. Both supported file-origin and Odoo-origin immutable
selections can reach offline preparation. Publication retains the last current
prepared snapshot until a replacement is complete.

### Comparison

`PreflightService.compare` loads exact current prepared/quality/normalization
evidence, plans bounded target reads, verifies the returned projection and
target identity, and atomically publishes comparison evidence. It does not run
source preparation inside comparison. Odoo-source pinned comparison retains its
separate protected three-way semantics.

## Focused verification

The following command completed successfully:

```console
.venv/bin/python -m unittest \
  tests.test_projects.ProjectLifecycleTests.test_pending_registry_summary_is_recovered_after_interrupted_write \
  tests.test_workspace.WorkspaceLifecycleTests.test_governed_mapping_revisions_and_submission_are_exact \
  tests.test_mapping_validation.MappingSemanticValidatorTests.test_source_choices_map_to_selection_keys_before_validation \
  tests.test_preparation_session.PreparationSessionRepositoryTests.test_prepared_snapshot_pointer_advances_only_after_publication \
  tests.test_preflight_service.PreflightPublicationTests.test_failed_repository_save_deletes_unpublished_manifest \
  tests.test_odoo_comparison.PinnedOdooComparisonTests.test_classifies_unchanged_update_and_concurrent_change \
  tests.test_recipe_phase_zero_contract
```

Result: 13 tests passed in 3.268 seconds.

## Phase 0 conclusion

The current boundaries support the clean-child-workspace direction but do not
yet support reusable recipes. Later phases must add their contracts and storage
without regressing the exact submission, publication, containment, bounded-read,
and evidence-currentness behavior above.

At baseline capture, the authoritative roadmap still made related/mixed
100,000-row preparation the unconditional priority. Product ownership later
recorded the explicit bounded priority exception required by the Phase 0 gate;
the completed Phase 1 report records that subsequent implementation.

## Related documentation

- [Phase 0 contracts](../plans/reusable-recipes-phase-0-contracts.md)
- [Phase 1 implementation report](reusable-recipes-phase-1-mapping-contract-2026-08-18.md)
- [Reusable recipes implementation plan](../plans/reusable-recipes-and-data-versions-implementation-plan.md)
- [Impodo remaining work](../plans/remaining-work.md)
- [Architecture overview](../architecture/overview.md)
