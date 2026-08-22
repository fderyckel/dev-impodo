# Migration Projects Phase M2 source-package foundation

## Status and authority

**Status:** Completed foundation phase from 2026-08-22.

This document records Phase M2 of the [Migration projects and multi-Recipe
cutover implementation
plan](migration-projects-and-multi-recipe-cutover-implementation-plan.md).
[ADR-014](../decisions/README.md#adr-014--migration-projects-coordinate-reusable-recipes-and-cutover-plans)
governs aggregate ownership. The [Phase M0
contracts](migration-projects-phase-m0-contracts.md) remain the executable
architecture boundary, and the [Phase M1
foundation](migration-projects-phase-m1-foundation.md) defines the clean roots
and recoverable storage layout that M2 extends.

M2 does not switch browser routes. The current Recipe-first browser continues
to use its current workspace stores until Phase M3 replaces creation and
composition. The M2 path does not read or dual-write those stores.

## 1. Implemented outcome

A `DataVersion` now owns one complete `DataVersionSourcePackage`. The package
contains:

- immutable file identities and content-addressed artifact references for a
  `FILE` source;
- hash-bound inspection catalogues and accepted parsing or table choices;
- logical dataset and mapping-column contracts;
- immutable snapshot hashes, storage references, and portable manifests; and
- one canonical package hash independent of caller collection order.

`DataVersionSourceIntakeService` streams each CSV or XLSX through the governed
artifact port under the DataVersion identity. It records only generated
storage keys and exact byte evidence. Failed registration deletes the newly
stored artifact. A draft may contain files before inspection, catalogues before
confirmation, and confirmations before snapshot publication; acceptance is the
first point that requires the complete chain.

An `ODOO` package uses the same DataVersion boundary but cannot contain file
evidence. Dataset source bindings must match the package origin. A file
dataset's binding must match the exact file, catalogue, and confirmation stored
in the same package.

The data manager may replace the complete package while its DataVersion is a
draft. Acceptance advances both package and DataVersion revisions, records the
exact package hash, and freezes the package. No command can edit or accept the
same frozen DataVersion under a new operation identity.

## 2. Workspace source projection

A `MigrationWorkspace` selects one or more logical datasets from its own
frozen DataVersion. The workspace store records only:

- the projection identity;
- the accepted package hash;
- selected dataset identities and snapshot hashes; and
- actor and creation evidence.

The repository resolves those references from the DataVersion store each time
it returns a `WorkspaceSourceProjection`. It rejects a missing, mutable,
cross-Project, hash-mismatched, or snapshot-mismatched package. The workspace
does not receive source files, catalogues, manifests, a copied DataVersion
database, or a mutable source-selection pointer.

`WorkspaceMappingSourceProjection` satisfies the existing mapping source port.
It converts only the selected immutable dataset contracts into the mapping
engine's read-only `SourceSelection` view. This is the application seam that
Phase M3 will compose into the browser; it does not create a second mapping
engine or persist a compatibility shape.

## 3. Exact M2 persistence

M2 keeps the M1 directory layout and replaces every schema generation exactly:

| Database | Generation | Added responsibility |
| --- | --- | --- |
| Registry | `impodo-migration-registry-2026-08-m2` | Generic revision-bound operation intents for DataVersion acceptance and workspace projection |
| DataVersion store | `impodo-data-version-store-2026-08-m2` | Source package state, files, catalogues, confirmations, dataset contracts, snapshot references, and source events |
| MigrationWorkspace store | `impodo-migration-workspace-2026-08-m2` | One immutable source projection and its selected dataset references |

The store validators require the exact tables, columns, generation, and root
linkage. M1 and Recipe-first storage fail closed. Impodo does not migrate,
backfill, lazily adopt, or mutate rejected development storage. The existing
reviewed development-reset command remains the only recovery path.

## 4. Concurrency, recovery, and performance

Draft replacement uses the package's own optimistic revision. Acceptance also
uses the DataVersion revision. Projection creation uses the workspace revision.
Stale commands fail without mutation.

Acceptance and projection reserve actor-bound, request-hash-bound operation
intents before crossing database boundaries. An equivalent retry completes or
returns the original result after a simulated interruption. Reusing an
operation identity with another actor, owner, revision, or request meaning
fails closed.

Projection materialization performs bounded root and package reads and loops
only over the selected dataset metadata in memory. It performs no Odoo read,
source-row loop, workspace fan-out, or per-Recipe query. The two-workspace gate
opens one DataVersion store and two independent workspace stores; it creates no
second DataVersion database.

## 5. Internal workspace name

The former current-browser class named `MigrationProject` is now
`WorkspaceState` across domain consumers, adapters, services, presenters,
scripts, and tests. `MigrationProject` now names only the clean operator-facing
business root in `migration_projects.py`. `MigrationWorkspace` remains the
clean isolated workspace root in `migration_workspaces.py`.

This rename is direct. The codebase retains no old type alias or compatibility
import.

## 6. Verification gate

[`tests/test_migration_project_phase_m2_source_packages.py`](../../tests/test_migration_project_phase_m2_source_packages.py)
proves:

- exact M2 schema generations and DataVersion-only source ownership;
- incremental file intake, inspection, confirmation, and dataset publication;
- file and Odoo packages sharing the same DataVersion lifecycle boundary;
- canonical package membership and hashes;
- immutable acceptance and exact DataVersion binding;
- two workspaces selecting different datasets from one package;
- mapping-port access to only each workspace's selected datasets;
- authorization and optimistic concurrency;
- non-mutating rejection of unknown datasets;
- restart-safe acceptance and projection after injected faults; and
- non-mutating rejection of M1 storage.

The M2 gate passes when this suite, the M0-M1 suites, and current workspace
regressions pass together. Phase M3 may then switch Project creation and
browser composition without an old-schema reader, dual-write path, or Recipe
shell.
