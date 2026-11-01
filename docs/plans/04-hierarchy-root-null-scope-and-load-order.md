# Qualify complete hierarchy migration and explain dependency failures

## Status and issue

**Status:** Core implementation and initial automated qualification were
recorded on 2026-10-30. Steps 1 and 2 were implemented on 2026-10-31; their
verification is recorded below. Step 3 was implemented on 2026-11-01;
its offline verification is recorded below. Steps 4 through 7 and live
disposable-target qualification remain open. This work is
second in the cross-plan order owned by
[the roadmap](00-remaining-work.md). The original plan records product-owner
approval of the fictional scenarios and initial implementation sequence.

A data manager must be able to migrate a hierarchy and the records that use it
without confusing identically named branches, treating a legitimate root as
incomplete, or loading children before their parents are safely available.
When something fails, Impodo must explain the original problem, identify its
full impact, and guide correction and recovery without losing accepted data
or duplicating records.

This plan is for the developer completing that qualification. The remaining
work fills specific coverage gaps, adds a repeatable live qualification runner,
and fixes defects exposed by those checks. It builds on the existing
preparation, comparison, execution, Recipe, recovery, and issue-projection
owners.

## Current foundations

- Generated roots use `explicit_scope_null` for their optional self-parent
  scope. Populated parents remain dependencies. Ordinary identity keys and
  partially blank composite keys remain required. Historical version-16
  mappings retain their original `reject` meaning.
- Preflight matches the complete lineage, ending with an unset parent at the
  root. It batches at most 500 lineage expressions per request group and never
  falls back to a leaf-only or unrestricted read.
- Frozen execution evidence orders parents before children and requires a
  durable create receipt before releasing a dependent write. Acyclic
  hierarchies need no later relationship-completion writes.
- Preparation publishes complete-run cause counts, parent, child, and sibling
  impact, and bounded affected-record membership. Stages 4 and 5 reuse that
  evidence for correction links and paged review.
- Qualified direct relational identities can run natively, including explicit
  null roots. Generated and unsupported shapes retain their own preparation
  limits. A large joined-workspace result does not qualify every hierarchy.
- Reviewed blank choices for optional Many2one relationships are implemented.
  They do not grant permission to leave an ordinary identity component blank.

Current behaviour belongs to [Match data](../developer/workflow/03-match-data.md),
[Prepare data](../developer/workflow/04-prepare-data.md),
[Final review](../developer/workflow/05-final-review.md), and
[Load into Odoo](../developer/workflow/06-load-into-odoo.md#current-relationship-ordering).
The [related-table guide](../user/guides/related-tables.md) owns hierarchy
authoring. This plan does not replace those current contracts.

## Recheck findings before steps 1 and 2

The initial recheck on 2026-10-31 found that the core behaviour existed. It
identified the following coverage and gaps before the completion work began.
The implementation record below supersedes this initial coverage inventory.

| Responsibility | Existing evidence | Completion gap |
| --- | --- | --- |
| Complete migration | `HierarchyWorkflowTests` exercises generated categories in four Odoo-major and connection-mode combinations. | Add the equivalent accepted-file journey for a supplied parent table. Assert the exact category of all six Products; the existing test explicitly checks only PX101 through PX103. |
| Recipe reuse | `HierarchyRecipeTests` rebinds a reordered delivery and verifies the generated hierarchy. | The dedicated case uses Odoo 19 LOCAL. Extend reuse across the four combinations and both source shapes. |
| Interruption and recovery | `HierarchyRecoveryTests` covers Odoo 19 LOCAL and one Odoo 19 REMOTE commit-before-receipt case. | Complete the recovery matrix, exercise real transports, and prove recovery after reopening persisted evidence. |
| Failure meaning and review | Domain and repository suites cover null roots, collisions, cycles, optional blanks, and group propagation. The BoM route suite covers overlapping causes and obsolete links. | Trace every scenario C case to a focused assertion. Add accepted-file coverage where separate unit tests do not prove the complete correction path. |
| Size and failure volume | `HierarchyWorkflowScaleTests` increases Products to 25,000 physical rows while retaining the same 12 categories. | Measure distinct hierarchy growth, supported depth, fan-out, and many affected groups. The existing measurement does not qualify those shapes. |
| Live and supported-host acceptance | This plan records fictional transports and one local macOS browser run. Existing CI owns portable and Linux server checks. | Add live hierarchy receipts and revision-specific supported-host results. Existing CI configuration alone is not a successful run. |

The existing [compatibility lab preparer](../../scripts/prepare_odoo_compatibility_lab.py)
requires Windows executables and creates its lab beneath `.tmp`. Its
[target manifest](../../fixtures/odoo-compatibility/lab-targets.json) records
separate Odoo 19, preview, and final Odoo 20 pins. The
[Odoo 20 runner](../../scripts/qualify_odoo20_reads.py) qualifies reads, and the
[Product hook runner](../../scripts/qualify_odoo19_product_create_hook.py)
qualifies a narrow create boundary. None is a complete live hierarchy runner.
Reuse their applicable setup and evidence patterns without treating those
receipts as hierarchy acceptance.

## Product and evidence boundaries

A Data version retains one accepted source delivery. A Recipe retains reusable
decisions. A workspace selects and prepares data, and migration runs retain
their exact comparison, approval, load, and recovery evidence. Correcting a
rule creates a new revision; it never rewrites accepted sources, failed runs,
historical hashes, or an earlier approval.

Use the existing shared issue projection and persisted cause membership.
Grouping cannot change eligibility or hide independent findings. A record can
belong to several causes, but a combined affected-record total must count its
identity once. Distinguish a direct finding from inherited consequences and
retain missing, ambiguous, excluded, and quarantined parent reasons.

Only incoming relationships used in identity or scope create reverse
identity-group propagation. A faulty child can therefore set aside its parent
and siblings. An ordinary relationship does not acquire this reverse meaning.

Keep the product deployment-neutral. Browser queries must be authorized,
freshness-checked, bounded, and based on committed evidence. Keep local
database work inside one synchronous operation; release it before Odoo calls.
Do not introduce another issue store, per-row Odoo queries, or a connection
cache. Preserve the existing worker and shared-server ownership boundaries.

## Fictional acceptance scenarios

All values below are invented. Test artifacts belong in operating-system
temporary directories. No fixture may require customer data or credentials.

### A. Generate and migrate a four-level category hierarchy

One accepted Product file contains these six rows. A blank means an empty
source cell. The reviewed rule uses the deepest populated level.

| Product reference | Category level 1 | Level 2 | Level 3 | Level 4 |
| --- | --- | --- | --- | --- |
| PX101 | Machines | Pumps | Service | Seals |
| PX102 | Machines | Valves | Service | Seals |
| PX103 | Maintenance | Pumps | Service | Seals |
| PX104 | Machines | Pumps | blank | blank |
| PX105 | Machines | blank | blank | blank |
| PX106 | Tools | blank | blank | blank |

The rule produces 12 categories and six products. The three Seals categories
stay distinct because their complete paths differ. The destination initially
contains exactly these five relevant categories with matching mapped values:

```text
Machines
Machines / Pumps
Machines / Pumps / Service
Machines / Pumps / Service / Seals
Maintenance
```

Expected comparison: five unchanged categories, seven new categories, and six
new products. Loading therefore performs 13 creates and no updates. Verify
each product's exact category, root nulls, every parent receipt, deterministic
order, zero relationship-completion writes, and complete physical-to-logical
source accounting. Odoo-generated ancillary records are outside these mapped
record totals.

Publish and apply the Recipe to a fresh accepted delivery with different local
identifiers and source ordering. Compare business results through portable
identities. Changed source evidence may have different hashes; repeated
evaluation of the same frozen evidence must remain deterministic.

### B. Explain one missing component and its complete BoM group

Two BoM headers, BA100 and BB200, have the following lines. Each line's identity
includes its parent BoM. All components except PX999-MISSING resolve uniquely.

| BoM | Line | Component | Quantity |
| --- | --- | --- | --- |
| BA100 | 10 | PX101 | 2 |
| BA100 | 20 | PX999-MISSING | 1 |
| BA100 | 30 | PX102 | 4 |
| BB200 | 10 | PX103 | 1 |
| BB200 | 20 | PX106 | 3 |

Among these seven BoM records, expect one direct problem, one affected parent,
and two affected siblings. Those four records are set aside; BB200 and its two
lines remain eligible for review. This does not authorize a partial load.

The review must explain the missing component, open the owning relationship
correction, and show all four affected records. Correcting the component and
preparing again releases the group while retaining the earlier run.

A second variant gives the same line two independent problems. Preserve both
causes, compute each cause's impact, and count the combined four-record closure
once. Also prove that an unsafe category child can affect ancestors and
siblings when its parent participates in identity, rather than promising
branch-only isolation.

### C. Preserve distinct failure meanings

Cover duplicate source roots, ambiguous destination roots, repeated leaf names,
missing parents, deliberately excluded parents, quarantined parents, incomplete
compound parent keys, and hierarchy identity cycles. No invalid key becomes a
root and no ambiguous record is selected arbitrarily. Test the separate
reviewed optional-relationship blank policy alongside root identity handling.

### D. Recover without duplicate parents or premature children

Interrupt a new parent create before and after its durable receipt. Include a
known rejection and an uncertain transport outcome. Children must wait until
the parent's exact outcome is established. Recovery must use exact read-back
and the existing journal and External ID rules; it cannot blindly retry an
unknown create. A changed destination parent or stale comparison must require
a new review rather than silently retargeting a child.

## Completion implementation plan

Implement steps 1 and 2 first. Step 3 then supplies the tooling for step 4.
Step 5 can proceed once the fixtures are stable. Step 6 collects host evidence
for the resulting revision, and step 7 closes the plan. A missing live target
blocks its acceptance cells; it does not block the offline implementation.

### 1. Freeze the acceptance matrix and complete the source fixtures

**Status:** Implemented. All eight workflow cases and all eight Recipe cases
passed on 2026-10-31.

Extend [the existing fixture](../../tests/support/hierarchy_workflow.py) and
[workflow suite](../../tests/integration/web/test_hierarchy_workflow.py).
Keep the independent expected paths outside the target simulator.

- Add a supplied `categories.csv` containing the same 12 categories, stable
  source keys, and explicit parent keys. Accept it with the six Products
  through the normal source routes. Save its mapping through the existing
  mapping service; do not seed a prepared result or execution snapshot.
- Run scenario A for generated and supplied categories on Odoo 19 LOCAL,
  Odoo 19 REMOTE, final Odoo 20 LOCAL, and final Odoo 20 REMOTE. These are eight
  deterministic workflow cases. Both source shapes must produce the same
  portable business identities despite different physical source accounting.
- Assert all six Product paths, all three null roots, five unchanged
  categories, 13 creates, zero updates, and 18 verified mapped records. Also
  assert frozen parent order, a durable receipt for each created parent before
  its dependent writes, zero relationship completions, and complete source
  accounting. Existing parents retain their reviewed target identity checks.
- Extend [Recipe reuse](../../tests/integration/web/test_hierarchy_recipe.py)
  across the same eight cases. Use new dataset identifiers and reordered
  rows. Repeated preparation of the same frozen evidence must preserve its
  semantic hashes. Compare different deliveries through portable business
  identities rather than requiring their source hashes to match.

**Exit:** All eight workflow cases and all eight Recipe cases pass with
independently specified expected records. The test names and retained results
identify the source shape, Odoo major, and connection mode.

### 2. Close failure, correction, and recovery coverage

**Status:** Implemented. Failure, correction, and all 36 recovery scenario
combinations passed on 2026-10-31.

Extend the existing hierarchy identity, cause, impact, and recovery suites.
Reuse the mapping compatibility, optional-relationship, identity-group, and
native-reference suites instead of duplicating their lower-level contracts.

- Map each scenario C condition to a named assertion. Add any missing case
  for duplicate source roots, ambiguous target roots, excluded parents,
  quarantined parents, incomplete compound keys, and cycles. An excluded or
  unsafe parent must never be reinterpreted as a null root. Preserve mapping
  version 16 bytes and `reject` semantics alongside current null policies.
- Exercise saved correction through the owning browser route for a missing
  component and overlapping causes. Verify direct, parent, child, and sibling
  membership, distinct combined totals, non-overlapping pages, authorization
  before evidence access, and rejection of obsolete or foreign cause links.
  Reprepare after correction and reopen the earlier run to prove it is intact.
- Parameterize scenario D over the four Odoo-major and connection-mode
  combinations. Distinguish interruption before target dispatch, after target
  commit but before a journal receipt, and after a durable receipt. Include
  known rejection, uncertain outcome, missing or ambiguous read-back, changed
  parent identity, and stale comparison evidence.
- Reopen the isolated workspace with a new application context for recovery.
  Re-establish its credential binding through the existing recovery flow.
  Prove that children wait, uncertain creates are not blindly retried, and
  REMOTE External ID recovery does not duplicate a parent.

If a test exposes a product defect, fix it at its existing owner. Null identity
parsing belongs to [preparation](../../src/impodo/domain/preparation/source.py).
Ancestor resolution belongs to
[execution lookups](../../src/impodo/application/workspace/execution/identity_lookups.py).
Cause publication belongs to the
[quality index](../../src/impodo/adapters/duckdb/preparation_quality_index.py)
and [quality repository](../../src/impodo/adapters/duckdb/quality_repository.py).
Correction links belong to the
[summary presenter](../../src/impodo/web/presenters/summary.py).
Preserve the existing execution service, journal, and reconciliation ownership.

**Exit:** Scenarios B through D have explicit passing assertions and historical
evidence remains readable. Changed browser operations retain their worker
scope and have a measured connection ceiling. The existing real cause-detail
fixture remains bounded at five connections and five schema checks unless an
owner change is separately justified by evidence.

### 3. Add a repeatable live hierarchy qualification runner

Implemented by `scripts/qualify_hierarchy_workflow.py` with focused tests under
`tests/integration/odoo/`. Keep its portable scenario logic separate from
host-specific service setup. Host-executing tests belong under
`tests/platform/<platform>/`; use operating-system temporary directories.

- Accept an explicit disposable-target descriptor, source shape, connection
  mode, expected Odoo major, and output directory. Read credentials through
  the established protected mechanism. Refuse a mismatched runtime version or
  a target without the required fixture modules and permissions.
- Use the recorded `baseline` and `odoo20` pins where the compatibility lab is
  used. Assert the reported runtime version. The preview pin cannot satisfy a
  final Odoo 20 cell. A native installation is valid; Docker is an option for
  supplying a disposable target rather than a prerequisite for every host.
- Create a fresh fixture database, or use an explicitly allocated empty
  disposable database. Seed only the five expected category paths. Keep any
  standard Odoo data outside the fixture identity scope and assert that it
  does not collide. Capture real schema, defaults, permissions, and reader
  identity through normal Impodo composition.
- Exercise acceptance, mapping, preparation, comparison, approval, execution,
  and reconciliation through normal routes and services. LOCAL comparison
  must use its local reader; REMOTE must use its JSON-2 reader. Use the real
  writer and read-back adapters. Do not inject `HierarchyOdoo.compare` or
  manufacture a live schema catalogue.
- Add fault injection around the real transport and receipt boundary for
  scenario D. Record that faults were injected. Verify actual target state
  after interruption and before recovery instead of inferring a commit from
  the injected exception.
- Retain a sanitized result for success, failure, and incomplete cleanup.
  Stop only services started by the runner and remove only its disposable
  fixture resources. Retain uncertain target state for diagnosis rather than
  claiming successful cleanup.

**Exit:** Offline runner tests prove version rejection, fixture isolation,
adapter selection, fault boundaries, result writing, and cleanup ownership.
The runner can execute a complete cell when a live target is supplied.
Preparing a database or passing these tests does not mark a live cell passed.

### 4. Execute the live acceptance matrix

Allocate a disposable target and suitable read and write identities for each
cell. Record its runtime version, source pin or image digest, fixture modules,
Impodo revision, and protected credential-binding evidence. Missing target
access is an external prerequisite, not evidence of product success or failure.

| Target | Connection mode | Required live evidence |
| --- | --- | --- |
| Odoo 19 | LOCAL | Generated and supplied scenario A, Recipe reuse, scenario D, and restricted-access rejection. |
| Odoo 19 | REMOTE | The same journey with JSON-2 reads and External ID recovery. |
| Final Odoo 20 | LOCAL | The same journey using final-version schema, defaults, and create hooks. |
| Final Odoo 20 | REMOTE | The same journey with JSON-2 reads and External ID recovery. |

Use a fresh seeded destination for each independent case and each Recipe
replay that expects 13 creates. Separately compare the completed destination
again and require 18 unchanged records with zero further writes. Verify the
exact parent IDs and Product category paths by read-back. Count generated
Product variants and other Odoo hook effects separately from the 18 mapped
records. Test an access restriction without replacing the reader with a more
privileged identity. Retain the failed comparison or write and prove that no
dependent write bypassed it.

**Exit:** Every cell has a live receipt for the reviewed revision. A simulator
result cannot fill a live cell. A failed cell creates a focused regression
test and a bounded fix before the affected cell is rerun.

### 5. Measure hierarchy growth and failure volume within current limits

Extend [the hierarchy scale suite](../../tests/performance/test_hierarchy_workflow_scale.py)
and reuse the existing dependency-execution and identity-group benchmark
owners. Keep larger cases opt-in.

- Retain the existing 1,000, 10,000, and 25,000-row Product measurements.
  Add generated hierarchies at two, four, and five levels, with growing
  distinct paths and wide sibling groups. Five is the current authoring
  maximum; this qualification does not raise it.
- Include more than 500 distinct lineage expressions to cross a comparison
  request boundary. Verify that ancestor lookups deduplicate shared parents,
  stay bounded, and never degrade to leaf-only or unrestricted matching.
- Add missing-component failures across many BoM groups and two independent
  causes in one group. Check complete membership, distinct totals, and bounded
  browser pages rather than measuring only blank Product identities.
- Record input and output counts, depth, distinct nodes, fan-out, failure
  density, elapsed time, peak process and worker memory, Odoo request counts,
  database connections, schema checks, retries, and evidence hashes. Measure
  preparation, comparison, load, and review separately where exercised.
- Assert business outcomes, request shape, and evidence-based connection
  ceilings in CI. Record wall-clock measurements without timing thresholds.
  Retime a restarted live build before making a live latency claim.

**Exit:** Retained reports cover hierarchy growth and failure volume within
each route's current envelope. The separate 100,000-row mixed and related
expansion remains with [the roadmap](00-remaining-work.md); it is not a new
completion requirement here. This plan does not raise the generated route's
25,000-physical-row ceiling or another route's limits.

### 6. Collect supported-host and shared-server evidence

Run the portable hierarchy suites on Linux, macOS, and Windows through the
existing [Python quality workflow](../../.github/workflows/python-quality.yml).
Attach revision-specific job results. The four live cells qualify the Odoo
major and transport; they do not require every live cell on every desktop OS.
Run one authenticated correction and affected-record browser journey on both
desktop hosts, and repeat the interruption-and-reopen check on each host.

For the Linux shared-server profile, add a hierarchy case beside the existing
[cross-store preparation tests](../../tests/platform/linux/test_preparation_cross_store.py).
Verify authorization, worker ownership, committed cause membership, replay,
and restart recovery through the existing PostgreSQL and DuckDB coordination.
Use the existing
[Linux workflow](../../.github/workflows/linux-server-quality.yml).
The broader server release remains owned by its deployment plan.

**Exit:** Host and shared-server results identify the tested revision and
runtime profile. A local macOS run does not fill Windows or Linux server cells.

### 7. Close documentation and delivery evidence

After behaviour is verified, update its owning Match data, Prepare data,
Final review, and Load into Odoo documentation only where the implementation
or qualified boundary changed. Review affected contracts, code documentation,
the workflow registry, and code map. Refresh screenshots only when the visible
decision changed, using authenticated fictional data at 1440 by 1024.

Retain a qualification index here with one result per matrix cell. Each result
must identify the source shape, Odoo runtime, connection mode, host profile,
fixture version, Impodo revision and any uncommitted source digest, execution
and reconciliation outcomes, interruption point, metrics, and cleanup result.
Keep secrets and protected target identifiers out of portable reports. Record
an unavailable cell as omitted and a failed check as failed.

Run focused tests for every changed responsibility, the pinned Python quality
command after Python changes, documentation checks, and the complete repository
gate before delivery. Every failure blocks delivery until corrected or
explicitly waived by the human for that named guardrail. Successful static
checks cannot replace a missing live receipt.

**Exit:** All required offline, live, browser, and host cells pass for the
reviewed implementation, and the complete repository gate passes. Preserve
the qualification record and reconcile the roadmap before retiring the plan.
Keep the plan open while required external evidence remains missing.

## Qualification record

The following implementation results and timings were recorded on 2026-10-30.
They are historical qualification evidence, separate from the 2026-10-31
recheck and the planned completion matrix above.

### Implemented corrections

- Comparison preserves nested governed parent identities instead of rejecting
  a resolved parent that itself has a parent. Incoming and target identity
  cycles produce blockers instead of recursion failure. Historical business
  references keep their original encoding when field metadata is absent.
- The shared execution lookup planner resolves distinct ancestors in bounded
  groups before the native adapters receive flat exact-parent domains. This
  preserves native JSON-2 lookup and read-back capabilities without requesting
  dotted result fields or making a request for every source row.
- Recipe publication requires only physical input columns. Application also
  binds regenerated columns on an already-bound physical dataset. A fresh
  Product file therefore needs no uploaded selected-category-path column.
- Complete quality publication preserves all causal edges instead of selecting
  an arbitrary unsafe child as the parent's cause. Role classification keeps
  real ancestors and descendants distinct from siblings while retaining the
  complete closure across ordinary relationships and identity groups.
- An obsolete cause link returns HTTP 404 after correction rather than an
  internal server error. Earlier preparation evidence remains readable.

### Automated scenario evidence

The [workflow suite](../../tests/integration/web/test_hierarchy_workflow.py)
passes for LOCAL and REMOTE connection modes on fictional Odoo 19 and final
Odoo 20. It exercises the real JSON-2 writer and read-back adapters against an
independent stateful fictional transport. Each case produces 12 categories and
six products, classifies five categories unchanged, creates 13 records without
an update or relationship completion, and publishes a verified 18-record
reconciliation result. These are adapter qualifications, not live Odoo runs.

The [Recipe suite](../../tests/integration/web/test_hierarchy_recipe.py)
accepts a reordered fresh delivery through the normal Test-run routes, rebinds
new dataset identifiers, regenerates the hierarchy, loads 13 records, and
verifies the same 18-record result. The
[cause suite](../../tests/integration/web/test_hierarchy_causes.py) proves the
four-record BoM group, distinct overlapping causes, bounded pages, correction
through another accepted source column, and unchanged historical evidence.
The [hierarchy impact suite](../../tests/integration/duckdb/test_hierarchy_cause_impact.py)
also proves two affected ancestors, one consuming child, and two siblings, and
retains group consequences reached through a faulty component.

The [recovery suite](../../tests/integration/web/test_hierarchy_recovery.py)
passes interruption before and after a durable receipt, remote External ID
recovery, known rejection, uncertain outcome, and changed-parent rejection.
The [identity suite](../../tests/domain/preparation/test_hierarchy_identity.py)
covers ambiguity, missing parents, partial compound keys, source and target
cycles, bounded lineage requests, and historical reference serialization.
The [lookup suite](../../tests/application/workspace/execution/test_identity_lookups.py)
proves deduplicated ancestor reads, bounded model pages, and rejection of
missing, ambiguous, or unreviewed parent paths.

The [browser suite](../../tests/e2e/test_hierarchy_review.py) passes in
authenticated headless Microsoft Edge at 1440 by 1024. It shows one direct
finding, three dependency consequences, and all four affected BoM records with
their direct, parent, and sibling roles. Playwright 1.64.1 was available in
the repository cache; the browser check required loopback-server permission
and no administrator, Accessibility, or Screen Recording changes.

### Controlled preparation measurements

The opt-in [scale suite](../../tests/performance/test_hierarchy_workflow_scale.py)
passes the generated-hierarchy route through its existing 25,000-physical-row
ceiling. One local macOS run recorded the following preparation measurements.
Peak memory is process RSS, not total machine or child-process memory.

| Physical rows | Prepared records | Quarantined | Seconds | Peak RSS, MiB |
| --- | --- | --- | --- | --- |
| 1,000 | 1,012 | 0 | 0.578 | 269.8 |
| 10,000 | 10,012 | 0 | 3.123 | 370.4 |
| 25,000 | 25,012 | 0 | 9.037 | 524.7 |
| 10,000 with 20% blank Product identities | 10,012 | 2,000 | 4.234 | 602.7 |

Each preparation used 13 physical connections, four schema checks, and zero
lock retries. The real cause-detail HTTP fixture stayed within five physical
connections and five schema checks, including its authorizing foundation
reads. These measurements do not raise another route's limits or claim live
Odoo or shared-server latency.

### Reproduce the checks

Use the checkout runner and its repository-local cache. The core workflow
checks need no customer data or live Odoo credentials:

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --group quality python -m unittest \
  tests.integration.web.test_hierarchy_workflow \
  tests.integration.web.test_hierarchy_recipe \
  tests.integration.web.test_hierarchy_causes \
  tests.integration.web.test_hierarchy_recovery \
  tests.integration.duckdb.test_hierarchy_cause_impact \
  tests.domain.preparation.test_hierarchy_identity \
  tests.application.workspace.execution.test_identity_lookups -v

IMPODO_RUN_HIERARCHY_SCALE=1 UV_CACHE_DIR=.uv-cache \
  .tools/uv/uv run --frozen --group quality python -m unittest \
  tests.performance.test_hierarchy_workflow_scale -v

IMPODO_BROWSER_CHANNEL=msedge UV_CACHE_DIR=.uv-cache \
  .tools/uv/uv run --frozen --group quality --with playwright==1.64.1 \
  python -m unittest tests.e2e.test_hierarchy_review -v
```

Long suites used a temporary copy of the current tree because concurrent
development correctly triggered the application-build restart guard in the
shared checkout. That copy used the installed pinned environment with
`--no-sync` and `PYTHONPATH=src:.`; no dependency downgrade or lockfile change
was needed. Initial combined-run errors were rerun against the stable copy.
The complete repository gate also passed in the shared checkout, including
Ruff, Basedpyright, compilation, all 85 architecture tests, documentation, code
documentation, and whitespace checks.

### External qualification not recorded on 2026-10-30

Docker was installed on the macOS host, but its daemon was unavailable. No
disposable live Odoo 19 or Odoo 20 database or approved remote target was
allocated for this work. Live local and remote same-major acceptance therefore
was omitted, including real model defaults, create hooks, and target access
rules. The existing 100,000-row relationship probe was separately opt-in and
was not run. Shared-server and other-host release evidence was also not
provided by those local tests. The completion plan above supplies the remaining
sequence without making that separate scale expansion a prerequisite. This
plan does not change the supported Odoo versions.

### Recheck validation before implementation on 2026-10-31

The seven core modules in the reproduction command passed all 26 tests in a
temporary snapshot of the working tree based on `e0e1fa0c`, including the
uncommitted files present when it was copied. The snapshot used the installed
pinned environment through the checkout runner with `--frozen --no-sync` and
`PYTHONPATH=src:.`. This result qualifies that captured tree, not subsequent
concurrent edits.

The first shared-checkout run stopped seven scenarios at file upload with
HTTP 422 and the application-build restart message. The snapshot rerun passed
those scenarios without disabling the guard. Documentation validation and all
five documentation architecture tests passed.

The first complete repository-gate attempt in the shared checkout stopped at
Ruff with six diagnostics in concurrently edited Python files:

- `application/workspace/mapping/order_service.py` had `I001` and two `F401`
  diagnostics for unused `RelationshipChoiceFilter` and `RelationshipChoicePage`
  imports.
- `application/workspace/mapping/relationship_choice_review.py` and
  `domain/relationship_health.py` each had `I001`.
- `domain/relationship_review.py` had `F821` for the unresolved
  `RelationshipChoice` return annotation.

These paths are relative to `src/impodo/`. After further concurrent edits, a
second attempt passed Ruff, direct Basedpyright with zero diagnostics,
compilation, and whitespace checks. It then ran 88 architecture tests and
failed `test_current_generation_uses_only_workspace_identity_shapes` in
`tests/architecture/test_workspace_schema_contract.py`: the test expected
`SCHEMA_VERSION == 2`, while the current value was `3`.

The second attempt stopped before the gate's documentation and code-documentation
commands. Documentation checks passed separately as recorded above; the
code-documentation check was omitted. Neither gate attempt passed. The owner
of the concurrent workspace-schema change must reconcile its schema contract
and rerun the complete gate before delivery. That recheck changed only this
plan and preserved the concurrent Python and test edits.

Live Odoo, browser, opt-in scale, Windows, and Linux shared-server runs were
not repeated for this plan edit. The earlier measurements remain historical;
the new completion matrix remains open.

### Steps 1 and 2 implementation on 2026-10-31

The [workflow suite](../../tests/integration/web/test_hierarchy_workflow.py)
now contains eight named cases. It accepts either generated categories or a
separate physical category table on both Odoo majors and connection modes.
The [Recipe suite](../../tests/integration/web/test_hierarchy_recipe.py)
contains the same eight combinations and accepts reordered fresh files with
new dataset identifiers. Both suites use production JSON-2 writers and
read-back adapters against the independent fictional transport.

Every case checks all six Product paths, all 12 category paths, three null
roots, physical source accounting, five unchanged records, 13 creates, zero
updates, and 18 verified records. The workflow cases repeat preparation and
compare semantic hashes. The saved execution order and a target-side observer
both prove that dependent writes wait for durable parent receipts.

The [cause suite](../../tests/integration/web/test_hierarchy_causes.py) now
follows the saved Match data action and submits real mapping commands to
correct a missing component and an invalid quantity. It checks overlapping
four-record closures, bounded pages, denied evidence access, an unavailable
cause from another workspace, obsolete links, and historical evidence after
reopening the application.

That journey exposed two presentation defects. The summary omitted the local
schema needed to locate an exact relationship. Transformed scalar findings
also retained an internal field name that the correction link did not resolve.
The summary now reads the captured schema in both connection modes and maps
internal field names through the owning dataset's rules. This changes the
correction destination without rewriting persisted evidence or changing
eligibility.

The [recovery suite](../../tests/integration/web/test_hierarchy_recovery.py)
contains 36 scenario combinations across Odoo 19 and final Odoo 20, LOCAL and
REMOTE. It covers interruption before dispatch, after target commit, and
after a durable receipt; known rejection; an uncertain committed create;
missing or ambiguous parent read-back; a retargeted parent; and a stale
comparison hash. Interrupted successful journeys rebuild the application
context against the same durable workspace. REMOTE recovery replaces a lost
loading key through the real recovery-check route and records reauthorization.
The read identity remains bound to the reviewed evidence. These are fictional
transport tests, not live Odoo acceptance.

Scenario C now has the following named evidence:

| Condition | Assertion owner |
| --- | --- |
| Duplicate source roots | `HierarchyFailureWorkflowTests.test_duplicate_source_root_sets_aside_its_group_without_choosing_one` checks accepted files and saved ambiguity. |
| Ambiguous destination roots | `HierarchyFailureWorkflowTests.test_ambiguous_destination_root_blocks_the_accepted_file_journey` blocks comparison before a journal or write. |
| Repeated leaf names | All eight workflow cases verify the three distinct Seals paths and all Product links. |
| Missing parents | `HierarchyFailureWorkflowTests.test_missing_source_parent_keeps_missing_reason_and_populated_scope` preserves the missing reason and populated child scope. |
| Deliberately excluded parents | `HierarchyFailureWorkflowTests.test_reviewed_parent_exclusion_never_promotes_its_children_to_roots` confirms the rows-to-use decision, retains one excluded physical parent, and sets aside its children. |
| Quarantined parents | `HierarchyFailureWorkflowTests.test_quarantined_parent_remains_a_saved_parent_consequence` preserves the parent consequence and populated child scope. |
| Blank ordinary keys and incomplete compound keys | `HierarchyIdentityTests.test_blank_ordinary_identity_is_required_even_for_a_null_root` and `test_partly_blank_compound_parent_never_becomes_a_root` keep those invalid keys blocked. |
| Incoming and target identity cycles | `HierarchyIdentityTests.test_incoming_identity_cycle_becomes_saved_blocker` and `test_target_parent_cycle_becomes_comparison_blocker` retain separate failure meanings. |
| Reviewed optional-relationship blanks | The existing [blank-choice contract](../../tests/domain/mapping/test_relationship_blank_values.py) and [optional-reference suite](../../tests/domain/preparation/test_optional_many2one.py) remain separate from nullable hierarchy identity. |
| Historical mapping and reference evidence | The [mapping compatibility test](../../tests/domain/mapping/test_compatibility.py) pins version-16 serialized bytes and semantic hash. The [native-reference suite](../../tests/integration/duckdb/test_native_reference_serialization.py) preserves captured historical projection bytes and hashes. |

The native-reference suite initially failed eight setup cases in a clean
snapshot because it required the ignored repository `.tmp` directory. Its
fixture now uses an operating-system temporary directory. All eight cases
then passed without creating that repository directory.

Controlled summary measurements used the same fictional BoM state with two
independent problems. Before and after the correction-link change, each
request used six physical connections, one schema check, and zero retries.
One before run took 0.182 seconds, including 46.8 ms connecting and 5.2 ms
checking schema. One after run took 0.258 seconds, including 61.8 ms connecting
and 7.5 ms checking schema. These concurrent local fixture timings are not a
latency improvement claim. The existing cause-detail ceiling remains five
connections and five schema checks.

The authenticated hierarchy browser case passed in headless Microsoft Edge
at 1440 by 1024 with cached Playwright 1.64.1. Its first attempt could not bind
the loopback port in the sandbox. The authorized isolated rerun passed without
changing the test or application guardrails.

Focused verification covered 126 distinct test methods across the hierarchy,
preparation, correction, recovery, combined-source, mapping-compatibility,
optional-relationship, identity-group, and native-reference suites. The first
97-method batch passed 89 methods and exposed the eight temporary-directory
setup failures described above. The native-reference rerun passed all eight.
The 29-method recovery and adjacent-workflow batch passed 28 methods. Its
remaining method had eight assertions at the wrong boundary: targeted
assessment validly checked the interrupted root without rejecting unrelated
ancestors. After the test was corrected to check resume, all eight variations
passed without an application recovery change. A separate 22-method contract
and presenter run passed, including the new pinned version-16 hashes.

Long behavioral runs used stable operating-system temporary copies of the
working tree and the installed pinned environment with `--frozen --no-sync`
and `PYTHONPATH=src:.`. This preserved the application-build restart guard
while other checkout work continued. The final repository gate applies to the
shared checkout. Live Odoo targets, opt-in scale probes, and the supported-host
CI matrix were not run for these two steps. Steps 3 through 7 remain open.

The complete repository gate subsequently passed in the shared checkout:
Ruff, direct Basedpyright with zero diagnostics, compilation, whitespace,
all 88 architecture tests, documentation, and code-documentation checks.
Earlier documentation-registry link failures were corrected before that
successful run. The existing user instructions already describe exact
correction actions, so their screenshots did not require replacement.

The complete focused set can be reproduced with the checkout runner:

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --group quality python -m unittest \
  tests.integration.web.test_hierarchy_workflow \
  tests.integration.web.test_hierarchy_recipe \
  tests.integration.web.test_hierarchy_causes \
  tests.integration.web.test_hierarchy_failures \
  tests.integration.web.test_hierarchy_recovery \
  tests.integration.web.test_summary_presenter \
  tests.integration.web.test_preparation_workflow \
  tests.integration.web.test_load_recovery \
  tests.integration.web.test_combined_hierarchy \
  tests.integration.duckdb.test_hierarchy_cause_impact \
  tests.integration.duckdb.test_native_reference_serialization \
  tests.integration.duckdb.test_identity_group_quality \
  tests.domain.preparation.test_hierarchy_identity \
  tests.domain.preparation.test_optional_many2one \
  tests.domain.mapping.test_compatibility \
  tests.domain.mapping.test_relationship_blank_values \
  tests.application.workspace.execution.test_identity_lookups -v
```

### Step 3 implementation on 2026-11-01

The [live runner](../../scripts/qualify_hierarchy_workflow.py) accepts a strict
allocated-target descriptor, expected final Odoo major, source shape, connection
mode, fresh output directory, explicit write flag, and optional fault. The
[runner instructions](../../scripts/README.md) define the descriptor and protected
credential requirements. No live target was allocated for this implementation;
step 4 remains open.

The runner creates an isolated local application and leaves provisioning and
Linux server administration with existing owners. It starts and stops no Odoo
or PostgreSQL service. The [target helper](../../scripts/hierarchy_qualification_target.py)
checks the runtime, compatibility-lab pin when supplied, modules, permissions,
and fixture collisions before seeding exactly five category paths. It uses
exact target-bound READ and WRITE vault envelopes and tracks only newly created
fixture entries. Exact Odoo IDs remain private.

The [journey helper](../../scripts/hierarchy_qualification_journey.py) uses normal
source acceptance, mapping, preparation, schema/default capture, comparison,
execution, and reconciliation. LOCAL comparisons retain the native reader;
REMOTE uses JSON-2. Integrated Recipe activation requires API-reader evidence,
so Recipe schema capture obtains that evidence through normal composition even
for a LOCAL target. No fictional schema or comparison is installed by the
runner. Both loads verify all 18 mapped records and six generated variants;
a repeat comparison requires 18 unchanged records before Recipe replay.

A successful interruption case reads target state before recovery, reopens the
same private application storage, and resumes the durable journal. Known
rejection and uncertain-outcome modes produce failed retained results. Cleanup
checks ownership and foreign dependents, removes exact fixture IDs in dependency
order, and verifies category, Product, variant, and External ID absence.
Uncertain state is retained. Portable receipts contain only fixed scenario
metadata, counts, static failure codes, and timings. Private protected evidence
and its encryption keys remain available for diagnosis.

The offline [runner suite](../../tests/integration/odoo/test_hierarchy_qualification.py)
exercises the real JSON-2 adapters at an independent HTTP boundary and replaces
host execution only in its LOCAL test. It covers authoring, Recipe replay,
reopened recovery, rejected versions and allocations, target and role binding,
permissions, archived or active collisions, fault timing, sanitized terminal receipts, partial
seed retention, and exact cleanup ownership. These tests exposed a local
company-context snapshot rejection; the bounded correction retains that context
without fabricating API principals and keeps partial API evidence invalid.
The [snapshot suite](../../tests/domain/execution/test_snapshot.py) covers the
round trip and rejection cases.

Grouping local comparison inputs reduced the controlled REMOTE journey from
29 to 12 physical DuckDB opens, while retaining five schema checks and zero lock
retries. The runner suite enforces those ceilings across initial comparison,
repeat comparison, and Recipe comparison. Every simulated HTTP or native-reader
call also asserts that no retained DuckDB scope is active. This is controlled
fixture evidence; no live latency or acceptance result is claimed.

Verification used operating-system temporary storage and stable source snapshots
while other checkout work continued. The 22 runner methods, 17 snapshot methods,
three execution company-context methods, 76 execution service and adapter
methods, and 45 adjacent reader/default/company and DuckDB lifecycle methods
passed across focused runs. Direct Ruff, direct Basedpyright for the new
scripts, compilation, CLI help, the Python quality command, documentation
checks and five documentation tests, and the complete repository gate passed.
The complete gate included 88 architecture tests. No browser screenshot,
live Odoo, Windows host, or Linux host execution was performed for step 3.
Earlier draft runs found runner setup and assertion errors; the recorded
passing runs followed their corrections.

Reproduce the new runner and snapshot checks from the repository root:

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --extra test --group quality \
  python -m unittest tests.integration.odoo.test_hierarchy_qualification \
  tests.domain.execution.test_snapshot \
  tests.application.workspace.execution.test_company_context -v
```

Run the Python and complete repository quality commands from `AGENTS.md` after
those focused checks. The maintained runner instructions describe live use;
passing the offline command above does not change any step 4 acceptance cell.
