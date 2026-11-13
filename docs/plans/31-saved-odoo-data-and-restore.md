---
audience: developer
document_type: plan
status: completed
---

# Save clean Odoo data and restore it after a database reset

This plan preserves selected clean Odoo data so that a data manager can restore
it after a test database reset without repeating the original AX migration.
The agreed product model has two levels: a reusable capture configuration and
the dated data versions captured with it. One **Update saved data** action
refreshes all configured data. One **Restore all** action restores a selected
version into a chosen compatible database.

The first implementation was completed on 12 November 2026. The current
[user guide](../user/guides/saved-odoo-data.md) and
[developer workflow](../developer/workflow/02-odoo-data.md#saved-odoo-data-and-repeat-restoration)
own its operating instructions, source ownership, and verification boundaries.
The remaining sections retain the accepted design behind that implementation.

## Delivered scope and qualification

Each capture configuration binds one source database and one explicit source
company. Preserve additional companies with separate configurations. One
**Update saved data** action captures all configured models for that set, and
one explicitly confirmed **Restore all** action checks and restores its selected
version. Saved destination profiles retain field policies and reviewed defaults.
Encrypted package download and independent-installation import preserve source
data with new local ownership and keys.

The delivered field surface remains bounded by the qualified capture policy.
Company-dependent property fields, translations, binary content and attachments
are excluded. A required relationship needs an admitted provider; an arbitrary
numeric default cannot supply it. Destination-only records and extra children
are preserved. There is no record deletion or whole-restore rollback.

Controlled tests cover Odoo 19 and final Odoo 20 with local and remote source
and destination fixtures, initial and repeated captures, checked restoration,
required defaults, independent-installation archives, and durable source
projection. Those results do not qualify every live ORM inverse or establish a
complete Chart of Accounts backup. Hosted platform and live round-trip
qualification remain separate.

Fresh browser screenshots were blocked by the host's browser execution and
native-access restrictions. The user guide uses a genuine existing source-setup
illustration, and the new capture script is retained for later visual review.

## Operator goal and two levels

An operator cleans accounting configuration in fictional `odoo-dev` for
Companies A and B. The operator saves a separate capture configuration for each
company and preserves its associated data. Before the next reset,
**Update saved data** captures the latest clean records for the selected set.
After the reset, **Restore all** loads a selected saved version into `odoo-test`,
using the remembered destination choices.

| Level | Contains | Main action |
| --- | --- | --- |
| Capture configuration | Source database and company, main models, fields, record filters, archived-record choices, related models, and relationship scope. | **Save configuration changes** creates a new configuration revision. |
| Saved data | Immutable records and relationships captured with one exact configuration revision, together with completeness and capture evidence. | **Update saved data** creates the next complete data version. |

A configuration can have several saved versions. Editing it does not alter
earlier versions. A failed refresh does not replace the last complete version.
Reading pages or reopening a project does not start a capture or an Odoo write.

Destination restore choices are remembered operational settings. They do not
introduce another level that the operator must author to capture data. A
restore run records the destination, selected data version, exact field
policies, checks, writes, and verification for one execution.

## Scope and recommended defaults

The first usable delivery must provide capture, refresh, offline restoration,
dependency handling, per-field policies, and verified repeat restoration. A
capture screen without an end-to-end restoration is an intermediate result.

| Decision | Recommended first-delivery behavior |
| --- | --- |
| Supported versions | Support Odoo 19 to Odoo 19 and final Odoo 20 to final Odoo 20. Check the actual destination schema and installed capabilities. |
| Source scope | Bind each configuration to an exact source connection and an explicit company selection. Keep `odoo-dev` and `odoo-test` configurations distinct. |
| Destination scope | Allow an explicitly chosen compatible destination and company mapping. Remember choices separately for each destination context. |
| Fields | Offer **All restorable fields** and explicit selection with exclusions. Explain every non-restorable field in a coverage report. |
| Default restore values | Use saved values for captured writable business fields. Preserve fields absent from the saved version on existing records. |
| Destination-only records | Keep them. Generic record deletion and database reset are outside the first delivery. |
| Refresh | Perform a complete bounded recapture of the selected scope. Incremental capture is deferred. |
| Retention | Keep saved versions until deliberate deletion. A successful load does not remove its reusable source. |
| Package portability | Provide a protected download and import contract that can work on another installation without the original source or its API key. |
| Attachments and technical history | Exclude attachments, binary content, chatter, and audit history from the initial coverage unless a qualified capability explicitly supports them. Show exclusions. |
| Execution | Preflight the complete selected scope before writing. Use dependency order, durable journals, and read-back. Do not promise a transaction spanning the entire restore. |

These are proposed defaults, not additional questions blocking preparation of
the implementation. A request to delete destination records requires its own
model-scoped design because deleting a record is not a field policy.

## Capture configuration and relationship coverage

Use captured schema metadata and the verified Odoo major to compile a reusable
configuration. Reuse shared compatibility and reference-policy functions.
Do not assume a relationship from its label or use an Odoo 19 alias as a
version-neutral default. The engine must work for unrelated business models
and eligible custom fields as well as accounting.

Every configuration revision must identify:

1. The source connection, supported major, explicit company scope, and language
   context where translatable values are admitted.
2. The selected main models, bounded record filters, and archived-record choice.
3. The field selection mode, explicit exclusions, and the resolved field list
   used for each capture. **All restorable fields** evaluates current metadata
   on each refresh; earlier versions retain their own resolved lists.
4. The approved relationship graph and how records enter each related model.
5. Portable identities, parent scopes, deterministic child ordering, and the
   evidence needed to reconstruct relationships on another database.
6. Capability coverage, prerequisite setup, unresolved exceptions, and resource
   limits. Exceeding a limit blocks completeness rather than truncating data.

| Relationship choice | Capture and restore meaning |
| --- | --- |
| Preserve referenced records | Include the records actually reached through approved relationships and their full approved restorable values. |
| Preserve a whole selected population | Include every record within that model's explicit filters and company scope. |
| Preserve owned children | Capture child records, parent links, portable identity, and ordering needed to reconstruct the selected business object. |
| Reuse destination setup | Capture portable reference meaning and check destination prerequisites; do not silently create or update protected setup. |
| Let Odoo generate values | Preserve the admitted inputs and verify generated behavior where qualified. |
| Exclude related data | Record the exclusion and reject any configuration whose required restoration dependencies remain uncovered. |

Following a link must not automatically capture unrelated transactions or the
entire related model. Automatically admitted new scalar fields can enter an
**All restorable fields** refresh. Newly discovered relationship edges require
a saved scope rule or one consolidated scope decision before expanding data.

Minimum creation of a related record's identity and required fields is not a
complete configuration backup. Use it only where the operator deliberately
chooses that narrower meaning. A preservation claim must account for all
selected restorable values and dependencies.

Classify each field as restored, generated, a destination prerequisite,
explicitly excluded, or unsupported. Ineligible fields must remain visible in
coverage. In particular, qualify company-dependent values, translatable
values, inverse-backed editable projections, decimal values, and shared
records before claiming a model's clean configuration is fully preserved.

## Company and database identities

Separate the source's provenance from the destination's current record
identities. Keep numeric Odoo IDs in protected instance-bound evidence. Never
interpret a source ID as a destination ID. Prefer valid stable external
identifiers and approved unique business keys; provide an explicit portable
identity policy when those are insufficient.

The same company name in two databases does not establish the same company.
Require a reviewed company mapping. Capture company-dependent values in their
actual company context and restore them in the mapped context. Capture shared
records once while preserving approved company membership. Do not narrow a
shared record to whichever company happens to be active.

A reset may preserve the server address and database name. The existing
connector's connection identity does not prove whether the underlying database
was recreated. The dedicated restore entry must establish fresh destination
schema, access, company, and matching evidence. Use a trusted generation marker
where available. Otherwise require an explicit recreated-database declaration
and a fresh run; never claim automatic generation detection from the URL or
from a database UUID alone.

Remember portable restore choices across resets, but bind approvals, runtime
crosswalks, write receipts, and recovery journals to the current target
generation or declared fresh binding. An interrupted run from the previous
database must not resume against its replacement. Preserve the ordinary
Odoo-source transfer guard against accidentally writing to its live source.

A known source reset also requires an explicit reviewed source rebind before
refreshing the preferred version. Establish new provenance and identity
meaning; reused numeric IDs must not associate new source records with the
previous database. A refresh from the recreated empty source must not silently
replace the preferred clean version. Capture review must distinguish a
legitimate empty selection from missing access or a changed source binding.

## Complete refresh and immutable publication

**Update saved data** is one resumable operation over the complete selected
scope. It reads source Odoo only. It does not change the source to manufacture
identifiers, or write to a restore destination.

1. Capture the configuration revision, source access, schema, company context,
   and current version pointers in a bounded local operation.
2. Close local database owners. Count and discover the bounded dependency
   population with grouped remote reads. Deduplicate shared records and cycles.
3. Stream each selected model and its protected relationship evidence to
   temporary artifacts. Preserve explicit nulls, false values, zeros, and
   ordering independently of portable identity.
4. Recheck membership and available change evidence across the complete scope.
   Report the capture start and finish times. Ordinary paginated API reads are
   not a transactionally simultaneous database snapshot. Reject detected drift;
   require a quiet capture window when stronger consistency is necessary or
   change evidence is insufficient for the promised completeness.
5. Recheck configuration, lifecycle, authorization, source bindings, and
   integrity before publication. Publish the complete manifest set and advance
   the latest complete pointer atomically through the owning repositories.
6. Show added, changed, and removed records relative to the previous version
   where stable identity permits comparison. Preserve both immutable versions.
   An unavailable identity comparison must remain explicit.

Failed or cancelled refreshes retain the previous complete version and expose
an actionable failure. Retrying the same operation must not create duplicate
versions. Configuration changes during capture reject stale publication.
Cleanup removes unreferenced temporary artifacts through explicit ownership.

## Destination compatibility and field policies

A restore uses the data version's captured fields, not today's capture
configuration. A remembered destination profile supplies the operator's bulk
choices. The plan pins the exact profile revision and snapshot version.

| Destination condition | Required behavior |
| --- | --- |
| New optional field | Omit it on updates; use a checked Odoo default or an approved provider on creates. |
| New required field | Use a validated default, fixed value, or reusable rule. Block creation when none can supply a valid value. |
| Removed saved field | Offer a remembered skip policy and list the excluded values. Preserve them in the original data version. |
| Renamed field | Require one explicit replacement mapping. Do not infer a rename from labels alone. |
| Changed field type, selection, or related model | Validate a qualified conversion or require an explicit mapping or exclusion. Do not silently coerce incompatible values. |
| Field became read-only or generated | Reclassify it through the current write policy and explain the resulting coverage. |
| Missing model or module | Resolve the prerequisite before writing any dependent scope. |
| Missing identity or essential relationship | Require a new approved resolution strategy; a general skip policy cannot bypass it. |

| Policy for a captured field | Existing record | Missing record |
| --- | --- | --- |
| Use saved value | Write the captured value when different. An admitted explicit empty value can clear the field. | Use the captured value, subject to creation requirements. |
| Keep destination value | Omit the field from the update. | Use the saved creation value or an approved default/provider. |
| Fill only when empty | Write only when the destination value is empty under the field's semantics. False and zero are meaningful values. | Use the saved creation value or an approved provider. |

Provide bulk presets for all fields and per-field exceptions. Distinguish a
field absent from a snapshot from a field captured with an empty value. Keep
identity matching separate from ordinary overwrite policies. A changed identity
value needs an explicit rename or rebinding strategy.

For Many2one fields, resolve the saved portable reference in the destination.
For Many2many fields, offer exact saved membership, keep destination membership,
or add missing saved links. Exact replacement must report removed links,
including a saved empty membership. It does not delete related records.
For **Fill only when empty**, a collection is empty only when it has no members.
Owned-child reconstruction must preserve parent scope and legitimate repeated
business values; it must not delete extra children without a separate approved
policy. When such extra children prevent an exact result, report the difference.

The same consolidated screen must present schema exceptions and field policies
for all selected models. It must not require a fresh mapping journey per model
or per record. Save resolved choices for the destination context.

## Restore all and verification

**Restore all** starts one job for all included models. The operator sees the
destination and company mapping beside the action. If saved policies cover
the current destination, that action can run checks and then execute within
its explicit authorization. Unresolved changes return to the consolidated
review before any writes. A page GET never starts restoration.

The job must first prove that every source identity resolves to zero or one
eligible destination record. It creates missing admitted records, reuses
unchanged records, and updates existing records under the field policies.
Ambiguous keys block; choosing the first name match is forbidden.

If several selected populations contain the same shared record, compile one
write intent. Identical values may coalesce. Contradictory values, company
mappings, or policies require a decision before execution. Do not use an
implicit last-selected-set-wins rule.

Compile and preflight the complete dependency graph before the first write.
Create prerequisites first and link records afterward when valid. A required
creation cycle that cannot be satisfied by an admitted Odoo operation blocks
the plan; do not invent placeholder business records.

Use the existing admitted write capability, deterministic dependency waves,
durable execution journal, uncertain-outcome handling, and independent
read-back. Recheck stale evidence before writes. Pause on material concurrent
changes or uncertain outcomes rather than replaying blind writes. Recovery
must work after restarting Impodo without the original source being reachable.

The result distinguishes verified writes, unchanged records, kept destination
values, excluded fields, unmet expectations, and unresolved outcomes. A run
with explicit exclusions may complete with exclusions; it must not claim to
have fully restored fields that no longer exist. Verification compares the
effective policy result, so **Keep destination value** is not misreported as a
failed overwrite. There is no whole-restore rollback guarantee.

## Persistence, export, and retention

Keep reusable configuration revisions separate from actual immutable source
data. Prefer a project-owned capture definition, existing DataVersion source
ownership, and new run/workspace instances for restoration. Resolve the exact
integration with current Odoo-source lifecycle states before adding stores;
do not duplicate source-data ownership or weaken immutable evidence.

Reuse Recipe compilation components where suitable, but do not add data rows,
credentials, or target receipts to Recipes. Current Odoo-source Recipe
publication is unsupported and cannot be treated as this feature's storage.

A downloadable package needs a versioned manifest, configuration revision,
model datasets, portable references, field coverage, integrity hashes, and
protected provenance where necessary. Exclude API keys and previous write
authorization. Define bounded import validation, encryption, key transfer or
recovery, and schema evolution before claiming portability. Copying the current
encrypted workspace files is insufficient when their keys belong to another
installation. Tampering or incomplete artifacts must fail before admission.

Use existing authorization boundaries for viewing, capturing, exporting,
restoring, and deleting sensitive data. The saved-data retention policy must
protect restore points from ordinary successful-load cleanup and inactivity
expiry. Show which saved versions and referenced runs a deliberate deletion
would affect. Continue enforcing storage budgets with a visible resolution
instead of silently deleting the last usable version.

## Existing components and implementation gaps

| Responsibility | Starting point | Work required |
| --- | --- | --- |
| Odoo capture | [Capture service](../../src/impodo/application/odoo_source_capture_service.py), [publication](../../src/impodo/application/odoo_capture_publication_service.py), and [dependency discovery](../../src/impodo/application/odoo_dependency_capture.py). | Reusable configuration revisions, complete refresh lifecycle, company coverage, and eligibility extensions. |
| Immutable source ownership | [Source packages](../../src/impodo/application/data_version/source_packages.py) and [source snapshots](../../src/impodo/application/data_version/source_snapshots.py). | Retained reusable versions, coherent manifests, portable package import/export, and explicit lifecycle integration. |
| Capture eligibility | [Capture predicates](../../src/impodo/domain/odoo_source_capture.py), [source policy](../../src/impodo/domain/odoo_source_policy.py), and [scope policy](../../src/impodo/domain/odoo_source_scope.py). | Qualify per-company values and report field completeness rather than silently accepting current exclusions. |
| Destination checks | [Transfer review](../../src/impodo/application/transfer_review_service.py), [preflight](../../src/impodo/application/transfer_preflight_service.py), and [execution compilation](../../src/impodo/application/transfer_execution_service.py). | Compile field policies, schema adaptations, effective expectations, and a combined restore intent. |
| Database reset | [Workbench destination binding](../../src/impodo/domain/workspace/workbench.py). | Dedicated offline-source restore admission and fresh target binding at the same address, while preserving ordinary transfer guards. |
| Reusable rules | [Recipe compiler](../../src/impodo/application/recipe_compilation_service.py). | Share suitable portable rule compilation; do not remove the Odoo-source rejection without a complete replacement contract. |
| Retention | [Retention domain](../../src/impodo/domain/shared/retention.py). | Protect deliberate saved versions from inactivity and successful-load deletion. |

## Implementation sequence

1. Define domain contracts and acceptance fixtures for configuration revisions,
   immutable data versions, company mappings, field coverage, restore policies,
   target binding, and portable packages. Resolve ownership with DataVersion
   and MigrationRun before persistence changes.
2. Add configuration and version persistence with optimistic revision checks,
   idempotent operations, retention, authorization, and forward schema upgrades.
3. Extend generic capture capabilities and dependency planning. Qualify actual
   CoA field coverage, shared company membership, code mappings, taxes and owned
   supporting lines. Include an unrelated model to prove the engine is generic.
4. Implement complete refresh jobs, consistency assessment, atomic publication,
   failure cleanup, and comparison with the previous immutable version.
5. Implement destination compilation and the consolidated policy review. Cover
   schema evolution, new required fields, stable matching, and shared records.
6. Integrate one restore job with existing execution, fresh target bindings,
   independent read-back, repeat-run idempotency, and recovery without source
   access. Finish protected package download/import and key portability.
7. Build the two-level browser journey using existing colours, CSS, components,
   and navigation conventions. Add **Update saved data**, **Restore all**,
   version history, and one progress/result view. Show only actionable exceptions.
8. Complete the release gates and controlled round-trip acceptance. Update the
   owning current documentation, contracts, workflow registry, code map, and
   authenticated fictional screenshots only as implementation becomes current.

These are implementation steps toward one usable feature. Do not advertise
partial storage or UI work as a working configuration backup.

## Performance and concurrency requirements

Stream hash-checked columnar snapshots. Group source reads and dependency
frontiers by model, company context, and bounded ID sets. Deduplicate reference
lookups and apply narrow fields and predicates. Use set-based DuckDB validation
for identity, comparisons, and relationship coverage. Do not introduce one
Odoo or DuckDB call per record, or materialize every model as Python objects.

Use one bounded synchronous local operation for each admission, projection,
or publication phase. Close local database owners before remote calls, awaits,
streaming, or job dispatch. Never cache connections across requests. Derive
physical connection, schema inspection, SQL, and remote call budgets from
fixtures. Exercise failed saves and retries as well as page reads. Report
controlled measurements separately from live performance evidence.

Concurrent captures must not overwrite one another's latest pointers or mix
configuration revisions. Concurrent restore runs with overlapping target
scope require admission checks and current-state revalidation. Preserve
unknown outcomes and require reconciliation before conflicting new writes.

## Acceptance and verification

The decisive test captures a clean selected configuration, removes access to
the original source, restores into a disposable compatible database, verifies
the selected business values and relationships, and repeats the restore with
zero duplicate creations. Repeat the test after a reset at the same endpoint
and database name. Old journals and numeric IDs must not enter the new run.

| Scenario | Required evidence |
| --- | --- |
| Configuration and data separation | Edits create configuration revisions; refresh creates data versions; old versions retain their exact resolved fields and data. |
| Complete refresh | All configured models and dependencies publish together; failure, cancellation, drift, and concurrent edits preserve the previous complete version. |
| Database separation | Independent dev/test sources and destination choices cannot share stale evidence; deliberate same-major cross-database restore works. |
| Company scope | Per-company values, shared records, membership, identical labels in different scopes, and unauthorized companies behave correctly. |
| Existing destination data | Field overwrite, keep, fill-empty, explicit blanks, false, zero, unchanged records, and missing creates produce their effective expected results. |
| Relationships | Many2one resolution, exact and additive Many2many membership, empty collections, owned children, repeated child values, cycles, and inaccessible dependencies are covered. |
| Schema evolution | Added, removed, renamed, incompatible, required, read-only, and missing-model cases get the defined bulk decisions and do not silently lose coverage. |
| Identity and overlaps | Missing keys, ambiguous keys, rename attempts, shared-record duplicates, and conflicting captured values cannot silently select or overwrite a peer. |
| Offline portability | Export/import works on another installation with supported key recovery; missing source access, tampering, missing artifacts, and resource-limit breaches are covered. |
| Retention and recovery | Restart, interrupted writes, lost replies, stale generations, explicit deletion, and successful-load cleanup retain the correct restore points and execution meaning. |
| Operator journey | First configuration, repeat refresh, rejected refresh, destination exceptions, one restore action, progress, completion, and return after restart are browser-tested. |

Run focused domain, application, DuckDB, adapter, browser-route, and UI tests.
Cover both Odoo majors and local and remote connectors separately. Preserve
file authoring, ordinary Odoo transfer, and Recipe-run behavior where shared
code changes. Record genuine live round-trip evidence independently for each
major; a fixture result is not live qualification.

Run the Python quality command after Python edits, the documentation checks
after documentation edits, and the complete repository gate before handing
off any implementation. Follow the current main-only integration and fixed
candidate rules. Keep hosted workflows manual and do not dispatch them without
an explicit request. Portable, performance, Linux-server, and Windows evidence
remain necessary where applicable for release qualification. Preserve the
running Impodo session and coordinate an approved restart only when needed.
