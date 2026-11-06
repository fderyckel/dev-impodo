---
audience: developer
kind: plan
status: proposed
---

# Fixture cleanup inventory and evidence

This appendix supports the proposal to give data managers and coding agents
the same fictional business data and exercises. It records the repository
and chat evidence reviewed on 6 November 2026. The actions below are proposed;
this review does not move data, replace accepted fixtures, or qualify a load.

The review covered tracked files under `fixtures/`, `examples/`, `scenarios/`,
`profiles/`, `tests/fixtures/`, and `docs/examples/`, their repository callers,
and selected relevant local chats. The reviewed chats are listed below.
Several longer chats have older pages beyond those reviewed here; this is
not a claim that every historical turn or failed attempt was inspected.

## What the existing directories own

The names overlap, but the files serve several distinct responsibilities.
The cleanup should separate business practice data from frozen contract
evidence and small tests that isolate one behavior.

| Location | Observed contents | Proposed action |
| --- | --- | --- |
| `fixtures/golden/` | One combined target snapshot supports the offline golden comparison. | Keep the snapshot with its small test inputs until all golden consumers migrate together. Do not turn it into a live Odoo seed. |
| `fixtures/migration-projects/current-contract/` | Two JSON fixtures describe project relationships and a retained Recipe envelope. | Keep as contract evidence. These are not user-uploadable source data. A later naming cleanup may move them into a clearly named contract-fixture directory with their callers. |
| `fixtures/odoo-compatibility/` | Six files pin the Odoo lab targets, dependency locks, and Odoo 19 evidence compatibility. | Keep separate from practice data. Preserve the original bytes and version identities. |
| `examples/golden/` | Four tiny CSVs exercise Create, Update, No change, Ambiguous, and Blocked outcomes. | Retain a minimal regression slice. Later move its inputs, profile, and snapshot as one reviewed change. |
| `examples/bom/` | Two tiny CSVs demonstrate symbolic parent and child references. | Retain for the focused compiler and planner tests; migrate the public learning entry to the shared suite after parity is established. |
| `profiles/examples/` | Four profiles serve the golden slice, BoM example, products profile, and generated representative qualification. | Keep consumed profiles; move each only with its inputs and callers. Do not classify a profile as obsolete from its name alone. |
| `scenarios/contact-read-only/v1/` | A one-contact definition, profile, source, and saved target provide an offline canary. | Keep as the smallest governed scenario until the replacement has an equivalent zero-network canary. |
| `scenarios/contact-round-trip/v1/` | A separate contact fixture drives a disposable write and repeat comparison. | Keep frozen; supersede with a shared-suite definition after equivalent write, read-back, and repeat evidence exists. |
| `scenarios/product-bom-headers-round-trip/v1/` and `scenarios/bom-lines-round-trip/v1/` | Two deliberately ordered write scenarios cover units, product templates, BoM headers, and then lines that resolve product variants. | Preserve the two-stage dependency and existing expected projections. Replace duplicated source ownership only after equivalent scenarios pass. |
| `scenarios/fixtures/file-products-and-boms/v1/` | Three generated workbooks duplicate the round-trip source workbooks. | Treat as the first candidate to retire after the generator and every caller have a canonical replacement. No committed scenario definition points to this directory in the reviewed search. |
| `scenarios/plw-manufacturing-preparation/v1/` | Four larger workbooks prepare 37,253 rows without Odoo. | Keep the current preparation regression while a synthetic scale replacement is built. Do not publish it as verified anonymous practice data without provenance review. |
| `docs/examples/impodo-end-to-end-training/` | Three CSVs support the current end-to-end tutorial. | Replace these downloads with a reviewed slice of the shared suite when that user journey is verified. Keep the current links until that replacement is delivered. |
| `tests/fixtures/preparation/native-projection-v3.json` | A frozen native preparation serialization fixture. | Keep as versioned persistence evidence; do not regenerate it from the practice corpus. |
| `tests/support/` | Python builders and browser harnesses construct narrowly scoped states. | Keep small builders for unit and integration tests. Reuse shared business data only when a test needs the complete business journey. |

The inspected directories contain 9 files under `fixtures/`, 6 under
`examples/`, 29 under `scenarios/`, 5 under `profiles/`, 1 under
`tests/fixtures/`, and 3 under `docs/examples/`. The source data footprint is
small enough that byte savings should not drive the design. Clear ownership,
repeatable exercises, and independently reviewed results matter more.

## Verified duplicates and the current containment rule

SHA-256 comparison found these byte-identical workbook copies:

| Workbook | Identical locations beneath `scenarios/` |
| --- | --- |
| `DEMO_Article.xlsx` | `fixtures/file-products-and-boms/v1/` and `product-bom-headers-round-trip/v1/fixtures/`. |
| `DEMO_BOMVersion.xlsx` | `fixtures/file-products-and-boms/v1/` and `product-bom-headers-round-trip/v1/fixtures/`. |
| `DEMO_BOM.xlsx` | `fixtures/file-products-and-boms/v1/`, `product-bom-headers-round-trip/v1/fixtures/`, and `bom-lines-round-trip/v1/fixtures/`. |

[`generate_product_bom_fixture.py`](../../scripts/generate_product_bom_fixture.py)
writes to the first directory. It does not update the two consumed scenario
bundles or their pinned hashes. This creates a maintenance trap: regenerating
the workbooks does not necessarily update what qualification runs consume.

The dependency locks named `requirements-windows-py312-preview.txt` and
`requirements-windows-py312-odoo20.txt` are also byte-identical. They still
represent distinct pinned lab targets in
[`lab-targets.json`](../../fixtures/odoo-compatibility/lab-targets.json).
Identical content alone does not justify deleting either version identity.

The scenario loader deliberately resolves every fixture, profile, and expected
target projection inside the directory containing its definition. It rejects
fixture symlinks and verifies hashes before execution. See
[`load_scenario` and `_contained_path`](../../src/impodo/adapters/scenarios/loader.py)
and the
[`loader tests`](../../tests/integration/scenarios/test_loader.py).
Replacing copies with `../` references or symlinks would violate this boundary.

A shared suite can preserve this rule without changing the loader. Put its
several scenario definition files directly in one versioned suite directory.
Each definition can reference the same contained `data/` directory, its own
profile beneath `profiles/`, and independently reviewed results beneath
`expected/`. The browser's exercises can use those same data files. Separate
exercise directories must not introduce escaping references merely to remove
copies.

## Consumers that a migration must preserve

| Responsibility | Current consumers and acceptance evidence |
| --- | --- |
| Golden comparison semantics | [`examples-and-edge-cases.md`](../developer/reference/examples-and-edge-cases.md), [`test_preflight_outputs.py`](../../tests/integration/artifacts/test_preflight_outputs.py), [`test_quality.py`](../../tests/domain/preparation/test_quality.py), [`test_snapshot.py`](../../tests/domain/execution/test_snapshot.py), [`test_catalog_metadata.py`](../../tests/domain/data_version/test_catalog_metadata.py), and [`test_planner.py`](../../tests/domain/execution/test_planner.py). |
| Tiny BoM preparation and planning | [`test_planner.py`](../../tests/domain/execution/test_planner.py) and [`test_reporting_cli.py`](../../tests/integration/artifacts/test_reporting_cli.py). |
| Products profile validation | [`test_profile_and_values.py`](../../tests/domain/recipe/test_profile_and_values.py) and [`test_planner.py`](../../tests/domain/execution/test_planner.py). |
| Generated 150-row representative migration | [`qualify_representative_migration.py`](../../scripts/qualify_representative_migration.py), [`p4_representative.yaml`](../../profiles/examples/p4_representative.yaml), and [`test_representative_workload.py`](../../tests/performance/test_representative_workload.py). The generator expects 125 creates, 20 updates, and 5 unchanged rows on its reviewed seed. |
| Project and retained Recipe contracts | [`test_contracts.py`](../../tests/domain/project/test_contracts.py) and [`test_integrated_recipe_runs.py`](../../tests/application/run/test_integrated_recipe_runs.py). |
| Odoo compatibility identities and lab setup | [`test_odoo_compatibility_baseline.py`](../../tests/domain/test_odoo_compatibility_baseline.py), [`prepare_odoo_compatibility_lab.py`](../../scripts/prepare_odoo_compatibility_lab.py), [`hierarchy_qualification_target.py`](../../scripts/hierarchy_qualification_target.py), and [`test_hierarchy_qualification.py`](../../tests/integration/odoo/test_hierarchy_qualification.py). |
| Contact preparation across stores | [`test_cross_store_preparation.py`](../../tests/integration/duckdb/test_cross_store_preparation.py) and the Linux-only [`test_preparation_cross_store.py`](../../tests/platform/linux/test_preparation_cross_store.py). |
| Larger manufacturing preparation | [`test_plw_manufacturing_preparation.py`](../../tests/integration/scenarios/test_plw_manufacturing_preparation.py) and the Linux cross-store preparation suite. The first asserts 2,841 products, 1,942 BoM headers, 15,142 lines, and 17,328 operations. |
| User practice downloads | [`end-to-end-training.md`](../user/tutorials/end-to-end-training.md) links directly to the three training CSVs. |
| Governed scenario operator workflow | [`scenario-trials.md`](../user/guides/scenario-trials.md) and [`scenario-qualification.md`](../developer/runbooks/scenario-qualification.md) describe the current scenario boundary and write safeguards. |
| Persistence serialization | [`test_native_reference_serialization.py`](../../tests/integration/duckdb/test_native_reference_serialization.py) consumes the native projection fixture. |

The golden example includes custom `x_uc.asset` and `x_uc.asset.line` models.
It proves generic comparison behavior against a snapshot. It is not a seed
that a user can load into unmodified Odoo. Keep that distinction explicit when
selecting a public practice dataset.

The existing two-stage Product and BoM scenarios also have a functional
purpose. Odoo creates a product variant from its template. The lines scenario
then resolves that variant by business key. Consolidating data ownership must
not collapse this into a comparison that assumes the new variants already
exist.

## Privacy and provenance findings

The small generated Product and BoM fixture has a reviewed generator with
fictional names and identifiers. The larger PLW workbooks have realistic
manufacturing fields and retained legacy-style identifiers. A complete
generator or anonymization record for those four larger workbooks was not
found in the reviewed tracked scripts. Their anonymous status is therefore
**unverified**, rather than proven unsafe or proven safe.

An aggregate workbook inspection found no email-like cells, formula cells,
external links, connections, comments, macros, or custom XML in those four
workbooks. Creator metadata is present. These observations do not establish
anonymity: business codes, product names, routes, dimensions, combinations of
values, and workbook properties also need review.

The proposed public suite should be synthetic from its origin. Its provenance
record should identify the generator or authored source, deterministic seed,
business rules, intended defects, and review. Use fabricated identities,
reserved example domains, and replacement metadata. Do not copy private chat
rows or production evidence into the corpus. Older observed failures should
be recreated as fictional cases with the same structure.

## Chat evidence to turn into fictional exercises

The following evidence comes from chat text and reported results. Historical
claims of passing checks are observations from those chats, not checks rerun
for this proposal. The appendix omits customer names, production codes, local
project identifiers, and source records.

| Reviewed chat and local ID | Observed problem or use case | Proposed exercise |
| --- | --- | --- |
| **Match customer files to res.partner** — `01a1843a-42e9-75f2-8e34-c02b0a793951` | Three customer files contribute to one model. Related company-chain records were created before the first customer was rejected; later customers were never attempted. An internal normalized grouping key was written as a display name, and the detailed rejection reason was discarded. | Combine customer tables, preserve original display spelling, inject a controlled partial rejection, inspect actionable retained diagnostics, and compare again in the same project. Assert zero duplicate parent creations and distinguish rejected rows from rows never attempted. |
| **Match customer files to res.partner** — same chat | A selected related record contained both name and company, but one path interpreted the compound value as a literal name. Optional blank values and populated unresolved values were also confused. | Include duplicate related names in different companies, optional blanks, explicit leave-blank choices, and invalid populated values. Assert that matching, preparation, Recipe application, comparison, and read-back preserve the same meaning. |
| **Match customer files to res.partner** — same chat | The API user's default company differed from the reviewed operation company. Checking-key changes and company refresh updated one evidence layer while leaving another stale, producing repeated failures after an earlier fix. | Use one reviewed company per load, a different API default, shared records, and parent-company references. Re-enter a checking key, reopen the project, and check twice. Reuse the Recipe under a second reviewed company with fresh evidence. |
| **Fix saved columns persistence** — `01a19274-7972-7ff3-a43c-a4561d7de646` | A preview was mistaken for a completed save. On return, the creation form hid the saved combination and did not restore its editable selections. A hierarchy from a secondary preparation-only table was hard to discover. | Save and reopen a combination, switch projects and return, edit columns and join choices, and create a category hierarchy from the secondary table. Assert the saved state and resulting invalidation, not just a successful POST. |
| **Check BOM source persistence** — `01a1a6f0-3972-7493-bec6-0bb153575a14` | A rejected supporting-choice save looked like persistence loss. A company-access refresh made earlier choices stale and sent the operator back through an unrelated-looking review. | Save an invalid choice and retain it visibly as unsaved; correct it and verify the saved receipt. Refresh access and retain still-valid choices while identifying only changed required decisions. |
| **Check BOM source persistence** — same chat | Active-header filtering left lines whose parents were inactive, absent, or blank. Some active parent business keys were duplicated. Source row identity was confused with display sequence. | Filter authoritative parents and have children follow that reviewed selection. Include missing and blank parents, duplicate active parents, a leading-space code, fractional line numbers, and repeated display positions. Assert separate reasons and deterministic lineage. |
| **Check BOM source persistence** — same chat | Existing product codes were searched against product names. The shared lookup failed, blocking every BoM group. Advice to load missing products was initially misleading because lookup failure did not prove absence. | Seed existing products with distinct codes and names. Deliberately choose the wrong lookup, diagnose it, select the verified business key, and compare again. Include both a safe remainder and a case where no writes remain. |
| **Check BOM source persistence** — same chat | The operator needed one action to set aside complete problem groups and needed the review workbook to separate filter exclusions from data issues. | Include one failed child that defers its complete BoM and siblings, one filtered parent group, and one unaffected group. Verify browser totals, workbook sections, and the retained correction reason. |
| **Remove legacy scripts and version work** — `01a192cd-2da4-7190-98ee-cb11ff956092` | The cleanup retained current fixture, screenshot, benchmark, and qualification tools. Purpose-based script renames preserved fixture identities and disposable-database safeguards. Concurrent edits also invalidated validation. | Inventory callers before deleting; preserve pinned evidence and current capture tools. Validate the final settled tree and record which replacement owns each removed artifact. |
| **Add containerized agent environment** — `01a178cb-06d8-70f3-89b6-f50c3af22b18` | The user corrected an initial Docker interpretation and wanted a local uv-managed environment. | Keep the suite usable through the checkout toolchain without Docker, administrator rights, or native browser Accessibility permissions. |

## Migration order and rollback

1. Record the current fixture hashes, consumers, and expected outcomes. Leave
   existing versioned bundles and contract snapshots unchanged.
2. Add the synthetic suite and independently reviewed row-level expectations.
   Give each exercise one source owner, a declared starting target state, an
   intended decision, and a verifiable result.
3. Add replacement definitions beside the old scenarios. Establish offline
   behavior first. Qualify affected live behavior separately for Odoo 19 and
   Odoo 20, local and remote access, authoring, and Recipe runs. Record omitted
   or failed qualification explicitly.
4. Move one consumer group at a time. Compare business outcomes, source
   lineage, relationship resolution, and repeat behavior. Do not overwrite old
   expected hashes merely because the new generator produces different bytes.
5. Update the current tutorial and download links only after the replacement
   user journey is verified. Update the generator output location and its
   consumers in the same change. Keep small isolated regression fixtures when
   replacing them would make a focused test depend on the whole suite.
6. Delete superseded duplicate copies only after repository references and
   documentation links have moved, replacement checks pass, and the complete
   repository gate passes. Preserve the old content in Git history; an
   additional archive directory would recreate the ownership problem.

Rollback should restore the old consumer references and definitions as one
change. Existing v1 scenario inputs and expectations remain available during
the transition, so rollback does not require rewriting accepted bytes or
editing execution evidence. A failed live attempt retains its journal and
uses the normal recovery contract; deleting or replacing a fixture must never
be used to make an uncertain write replayable.

This appendix was produced through read-only inspection. No existing fixture,
application state, Impodo session, or Odoo target was modified or exercised.
