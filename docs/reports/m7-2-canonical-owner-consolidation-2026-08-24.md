---
audience: developer
kind: report
status: current
---

# M7.2 canonical owner consolidation

**Historical evidence:** This dated report records one completed delivery
slice. Current architecture and lifecycle contracts own behavior.

## Outcome

M7.2 makes each setup value answer to one canonical owner:

- `MigrationWorkspace` owns workspace identity, open or closed lifecycle,
  `DRAFT` or `READY` setup state, completion time, and optimistic revision.
- `MigrationProject` owns the business name, purpose, source-system identity,
  data classification, and retention policy.
- `DataVersionSourcePackage` owns draft and frozen source origin, file
  references, inspection catalogues, confirmed parsing choices, datasets, and
  snapshot references.
- `MigrationRunTargetSetup` owns the mutable Local or Remote Odoo choice before
  capture. The existing `TargetBinding` remains the immutable run target.
- `MigrationWorkspace` and its engine own only selected dataset references and
  current mapping and operational evidence.

The browser still uses a flat `WorkspaceState` workbench API until the M7.4 and
M7.5 naming cutovers. That API is now documented and implemented as a derived
projection, not as another identity or aggregate root.

## Implemented boundary

The exact registry generation now stores workspace setup state and one
optimistically versioned target-setup row per MigrationRun. Completing setup
advances the clean workspace root. Two workspaces in one run therefore read the
same target choice.

Source upload writes the draft DataVersion package before it writes the local
workbench cache. Inspection and confirmation reads and writes use
`DataVersionOwnedSourceRepository`. Final source acceptance adds datasets and
snapshot references to the existing package instead of reconstructing source
ownership from workspace tables.

`WorkspaceOwnerViewService` gives presenters explicit Project,
MigrationWorkspace, DataVersion, MigrationRun, source-package, and target-setup
objects. It validates their lineage through the bounded M7.1 access context.
M7.3 subsequently made that context mandatory at workspace route and Odoo-job
entry points before any workspace or external boundary opens.

Inactive Project-details and governance templates, setup-step registrations,
service methods, navigation links, schema fields, manifest fields, and test
fixtures were deleted. The removed manager, functional-owner, business-unit,
support-access, export-status, export-date, and free-description values had no
current browser command or canonical business owner.

## Cache rule

The retained workspace-engine file and target columns support current
invalidation and isolated-worker behavior. They are derived caches only:

1. a current page reads Project values from `MigrationProject`;
2. it reads source values from the DataVersion package;
3. it reads target setup from the MigrationRun; and
4. tampering with a duplicate workbench value cannot change the rendered
   canonical value.

M7.4 subsequently removed the remaining `project_id` alias and cut the
workspace evidence schema, payloads, artifacts, and hashes over to
`workspace_id`.

## Verification evidence

The following focused checks passed on 2026-08-24:

- the then-current identity, workspace-access, and canonical-owner gate, now
  retained under `tests.test_identity_semantics`, `tests.test_workspace_access`,
  and `tests.test_canonical_ownership` — 15 tests at this slice;
- `python -m unittest tests.test_workspace -v` — 16 tests;
- `python -m unittest tests.test_preparation_session -v` — 3 tests;
- the integrated multi-Recipe gate, now retained under
  `tests.test_integrated_recipe_runs` — 8 tests;
- the M3 file-acceptance test;
- the browser Odoo-source setup test;
- the browser source-file correction and freeze-boundary test;
- `python -m compileall -q src/impodo tests`; and
- `python scripts/documentation_quality.py --check`.

The combined M1–M3 command exceeded its 120-second command limit after the M1
and M2 suites passed and while M3 was running. The previously reported M3 file
failure was corrected and its exact test then passed separately.

The workspace suite first stopped before its assertions because the Windows
sandbox denied access to newly created `.tmp` directories. The unchanged suite
then passed outside that filesystem sandbox. The longer
`test_complete_project_setup_registration_without_yaml` browser scenario
passed its setup, registration, contract-version, and removed-field assertions,
but later returned a failed background preparation job. It is not counted as a
passing M7.2 gate; no canonical-owner assertion failed in that scenario.

## Subsequent gate

M7.3 subsequently enforced the M7.1 parent-Project authorization context before
workspace stores, DataVersion evidence, credentials, protected artifacts,
background jobs, and Odoo adapters open. M7.4 then cut workspace evidence and
persistence from the temporary `project_id` alias to `workspace_id`.
