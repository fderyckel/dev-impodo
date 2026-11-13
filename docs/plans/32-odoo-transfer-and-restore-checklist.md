---
audience: developer
document_type: plan
status: partially-implemented
---

# Complete Odoo transfer and saved-data restoration

## Purpose and ownership

A data manager can move selected records between compatible Odoo databases or
preserve clean records and restore them after a test reset. These are two
operator journeys over one shared migration engine. For example, the same
parent-scoped matching rule must identify a line under the correct document
whether the source was captured immediately or imported from a saved package.

This checklist reconciles [ordinary Odoo transfer](05-odoo-to-odoo-migration.md)
and [saved Odoo data and restoration](31-saved-odoo-data-and-restore.md) as of
2026-11-13. It owns their shared completion tracking, rather than creating a
third engine or changing the [cross-plan priority](00-remaining-work.md).

| Owning plan | Responsibility |
| --- | --- |
| Plan 05 | Shared source capture, portable identities, dependency planning, destination matching, create-field checks, execution, read-back and generic transfer qualification. |
| Plan 31 | Reusable capture configurations, immutable saved versions, remembered restore policies, reset admission, source rebind, protected packages and retention. |
| Both entry points | Acceptance coverage when a shared capability changes. File authoring and Recipe runs also retain regression coverage when their shared components are affected. |

The implementation and accepted contracts remain authoritative. A test fixture
establishes its controlled boundary; it does not establish live ORM behavior,
Windows performance or release qualification. Record implementation, controlled
coverage and live evidence separately. Keep failed, interrupted and unavailable
checks visible. Update an item only after its owning behavior is verified.

## Implemented baseline

The following capabilities should be reused. Their remaining breadth and
qualification work belongs to the next section.

| Capability | Implementation and controlled evidence | Recorded live evidence and boundary |
| --- | --- | --- |
| Bounded immutable source capture | [Capture publication tests](../../tests/application/data_version/test_odoo_capture_publication.py) exercise complete publication and rejection of incomplete selected links. [Capture policy](../../src/impodo/domain/odoo_source_policy.py) retains the 10,000-row limit per model. | The recorded Contact trials establish selected scenarios, not every model, field surface or dependency graph. |
| Exact destination matching in batches | [Destination matching](../../src/impodo/application/destination_matching_service.py) uses at most 500 exact keys per request and a 501-result truncation sentinel. The [matching tests](../../tests/application/workspace/test_destination_matching.py) cover 1,205 identities split into 500, 500 and 205. | The former 1,000-total-key restriction is obsolete. The interactive 1,000-choice value control is separate. End-to-end measurements at the current source boundary remain required. |
| Direct existing-parent scope | **Match within** carries one captured writable stored Many2one to a selected unscoped parent through matching, review and the existing execution scope. [Backend tests](../../tests/application/workspace/test_scoped_odoo_transfer.py) cover Odoo 19/20 with local/remote metadata; [route tests](../../tests/integration/web/test_transfer_matching_scope_choices.py) cover ordinary choices and saved profiles. [Saved-policy tests](../../tests/application/workspace/test_scoped_saved_odoo_transfer.py) also exercise fresh preflight, compilation and independent reconciliation with wrong-parent detection. | Controlled existing-parent evidence only. Broader relational scope, actual scoped writes and a complete browser saved-restore round trip, live ORM behavior, fresh visual capture and latency qualification remain pending. |
| Required create-field providers | Shared matching and [transfer compilation tests](../../tests/application/workspace/test_transfer_execution.py) cover reviewed defaults, fixed values, source fields and governed existing or incoming references. Create-only providers do not update reused rows. | The Odoo 19 Contact vertical covered its exact reviewed defaults. It does not qualify every create hook or generated record. |
| Journalled execution, read-back and recovery | [Execution](../../src/impodo/application/transfer_execution_service.py) compiles approved rows into the shared writer and recovery contracts. [Preflight tests](../../tests/application/workspace/test_transfer_preflight.py) cover fresh read-only checks. | The [load workflow](../developer/workflow/06-load-into-odoo.md) records Odoo 19 Contact relationship qualification and Odoo 20 scalar Contact qualification on 2026-10-31 in both LOCAL-to-REMOTE and REMOTE-to-LOCAL directions, including accepted-batch interruption, durable reopen and original-journal recovery. |
| Saved configurations, versions and field policies | [Saved registry tests](../../tests/integration/duckdb/test_odoo_saved_data_repository.py), [restore policy tests](../../tests/domain/test_odoo_restore.py) and [shared transfer tests](../../tests/application/workspace/test_saved_odoo_transfer.py) cover immutable versions, profiles, overwrite/keep/fill policies and effective verification. | The [saved-data workflow](../developer/workflow/02-odoo-data.md#saved-odoo-data-and-repeat-restoration) records controlled Odoo 19/20 and local/remote fixture coverage. It does not claim a complete configuration backup or live ORM qualification. |
| Offline projection, repeat refresh and protected packages | [Source projection](../../tests/integration/web/test_saved_odoo_workspace_projection.py), [refresh lifecycle](../../tests/integration/web/test_saved_odoo_data_refresh_lifecycle.py), [archive lifecycle](../../tests/integration/web/test_saved_odoo_archive_lifecycle.py) and [restore flow](../../tests/integration/web/test_saved_odoo_restore_flow.py) tests cover the admitted operations. | Independent-installation import proves new local ownership and keys. Join that import to actual restore execution, repeat restoration and reset recovery before claiming the complete acceptance scenario. |
| Retained restore points | Saved versions are pinned against ordinary expiry and successful-load cleanup. The [registry tests](../../tests/integration/duckdb/test_odoo_saved_data_repository.py) deliberately block project deletion while saved data remains. | This establishes preservation. It does not supply deliberate version removal or a storage-management journey. |

## Remaining work in delivery order

This order applies within the two plans. Each item must carry its exact
candidate, commands, workload, environment and outcome when verification is
recorded. Missing tests and unimplemented behavior are different findings.

| Order and item | Owner | Current state | Completion action |
| --- | --- | --- | --- |
| 1. Match records within their parent or company. | Plan 05; verify both journeys. | Partially implemented: one direct captured writable stored Many2one to an unscoped parent already uniquely present in the destination. Null, missing/new, ambiguous or recursively scoped parents and inbound links to a scoped model are blocked. Product Category retains its governed complete-path behavior. | Qualify the complete saved-restore and recovery journeys for this slice. Extend portable scope deliberately for newly created or nested parents and inbound references. Retain ambiguity checks and legitimate repeated business values. |
| 2. Map source companies to destination companies explicitly. | Plan 05 engine; Plan 31 saved choices. | Choosing the operating company does not remap saved company relationships. The current [company regression](../../tests/application/workspace/test_saved_odoo_transfer.py) deliberately blocks a differently named destination company. | Add a reviewed mapping bound to exact source and destination evidence. Preserve shared membership, block unauthorized or ambiguous mappings and invalidate stale choices. Apply it through both entry points. |
| 3. Complete reset admission and source rebind. | Plan 31, with shared recovery regression. | Fresh restore workspaces exist. The local generation derived from an operation identity is not proof that the actual Odoo database was recreated. Explicit reset declaration and reviewed source rebind remain open. | Define a trusted generation marker or explicit recreated-database declaration. Prove that old approvals and journals cannot resume against a replacement database, even when the endpoint, database name and numeric IDs are reused. Require reviewed source rebind before refreshing from a recreated source. |
| 4. Qualify Product and BOM transfer breadth. | Plan 05; reuse scenarios in Plan 31. | Shared file/prepared-data execution has Product/BOM evidence. Complete Odoo-source Product/BOM transfer and multi-model recovery remain unqualified. | Exercise variants generated by Odoo, lines, operations, required dependencies, empty and partly populated destinations, rejected writes, lost replies and repeated runs. Include an unrelated model to prove generic behavior. Unsupported workflows must block before writes. |
| 5. Join offline portability and recovery acceptance. | Plan 31. | Separate controlled tests cover archive import, source-free projection, checked restoration and retry. The complete joined sequence still needs evidence. | Import on an independent installation, remove source access, restore, read back, repeat without duplicate creates, interrupt, reopen and recover the original journal. Repeat after a destination reset at the same endpoint. Check conflicting target scope across saved configurations. |
| 6. Finish saved-version management. | Plan 31. | Added/changed/removed comparisons and deliberate saved-version removal are open. The version table currently shows dates, configuration revisions, model counts and package download. | Compare stable identities between immutable versions, report unavailable comparisons, and preserve both versions. Provide authorized removal with affected-run disclosure and visible storage-budget resolution. Never silently delete the last usable version. |
| 7. Finish capture and rule-reuse decisions. | Plan 05; Plan 31 consumes capture choices. | The filter domain has more operators than the normal one-field equality browser form. Saved capture and restore settings exist; ordinary ad hoc transfer reuse needs separate verification. | Expose the accepted bounded filter choices and qualify their source binding. Check what ordinary transfer can reuse before adding another settings store. Preserve separate source data, reusable rules and destination approvals. |
| 8. Close visual, live and platform qualification. | Both plans, within existing release ownership. | Fresh saved-data screenshots, broader live ORM cases, realistic multi-model measurements and applicable platform evidence remain pending. | Run authenticated fictional browser journeys, independent same-major live trials and controlled Python 3.12 measurements. Retain separate macOS and real Windows evidence. Run the complete repository gate and applicable portable, Linux-server and Windows qualification before the corresponding delivery or release claim. |

The first engineering increment admits direct existing-parent scope. Its
controlled checks do not complete item 1's broader relational contract or live
qualification. Explicit company mapping in item 2 remains unimplemented. Keep
each remaining item open until its complete affected contract and focused
tests pass.

### Planned first company-mapping increment

This increment is **not implemented**. A data manager will explicitly match a
source company to a differently named existing destination company. Implement
that choice as a generic alias for a record identified by one scalar field in
a `reference_only` or `reuse_only` dataset. An unrelated reference model must
pass the same checks. Reject missing or ambiguous targets, unavailable source
choices, and mappings that merge distinct source records. Preserve the frozen
source identities and prohibit writes to the aliased reference records.

Store raw choices in encrypted, authorized evidence; public plans retain only
their bindings and counts. Carry the exact reviewed choice through ordinary
transfer and saved restoration, matching, approval, preflight, execution,
independent read-back and recovery after restart. Remember portable choices
separately from numeric destination IDs and previous write authorization.
Complete those paths and their Odoo 19/20 regressions together before admitting
the feature; a standalone mapping helper does not complete this increment.

## Shared acceptance sequence

Use one small fictional multi-model source with two parents that share a child
key and two companies that share a business label. Include an admitted
unrelated model. Capture explicit false, zero and empty values. Keep ordinary
business values and row ordering separate from the portable identity.

1. Capture the reviewed source scope without writes. Confirm complete model
   and relationship coverage, bounded reads and atomic publication.
2. Transfer into an empty and a partly populated compatible destination.
   Resolve parents and companies by reviewed portable evidence. Prove unique,
   missing and ambiguous matches, exact existing-row reuse and dependency order.
3. Save and export the same data. Import it into an independent installation
   with new local keys and identities. Disable the original source access.
4. Restore through remembered field policies and current destination checks.
   Independently verify intended values, links, kept values and explicit
   exclusions. Preserve destination-only records and extra children.
5. Repeat the transfer and restore. Assert zero duplicate creations and stable
   relationships. Recheck source, schema, company, policy and target drift.
6. Interrupt after an accepted write or lose its reply. Reopen durable state
   and recover the original journal and exact execution snapshot without
   contacting the source. Stop conflicting writes until the outcome is known.
7. Reset the destination while retaining its address and database name. Reuse
   some numeric IDs deliberately. Start a reviewed fresh restoration and prove
   that the old journal and approval cannot authorize work in the new database.
8. Recreate the source. Require reviewed rebind before refresh. Retain the
   preferred clean version when a refresh is empty or its provenance is unclear.

Record Odoo 19 and final Odoo 20 independently. Cover affected local and remote
source and destination access combinations. When a shared component also serves
file authoring or Recipe runs, retain focused coverage of those callers. A
single major, connector mode or entry point does not establish the others.

## Measurement and evidence rules

Measure the exact capture, destination check, restore, repeat and recovery
states used for correctness. Retain end-to-end and phase duration, CPU time,
sampled memory, Odoo requests, DuckDB connections, SQL and schema checks, lock
retries, bytes read and requested/effective concurrency where applicable.
Distinguish first and repeat runs. Repeat controlled concurrency measurements
at least three times on the same host and workload, changing one setting at a
time. Measure diagnostic overhead separately.

Keep local database work inside bounded synchronous operations. Close owners
before remote calls or job dispatch. Use grouped remote reads and columnar
source artifacts, with evidence-based request and SQL budgets. Do not add a
connection or lookup per record, or a cross-request connection cache.

Keep user diagnostic observations outside Git under the existing performance
history rules. Record missing measurements as unavailable. Compare builds
only when machine, workload, runtime and configuration establish comparability.
A macOS fixture or simulated Windows branch does not qualify Windows latency.
This checklist raises no row limit or concurrency default.

## Excluded and separately governed scope

- Cross-major transfer, whole-database upgrade and Odoo-source Production
  remain outside the admitted transfer lifecycle.
- Company-dependent property fields, translations, binary content and
  attachments are excluded from the delivered saved capture surface. Supporting
  them requires separately qualified capability extensions. The label
  **All restorable fields** does not imply support for those values.
- Arbitrary business actions and generated-record side effects require an
  admitted operation contract. The current `state` metadata guard does not
  detect every workflow.
- Restoration does not delete destination-only records or extra children and
  does not promise rollback of the complete job. Deliberate saved-version
  removal concerns retained Impodo data, not deletion of Odoo records.
- Live writes, remote deployment, hosted jobs and release publication retain
  their existing explicit authorization and qualification requirements. Keep
  hosted workflows manual; do not dispatch them to complete this checklist
  without a separate request.
