# Qualify complete hierarchy migration and explain dependency failures

## Status and issue

**Status:** Core implementation and automated qualification completed on
2026-10-30; live disposable-target qualification remains open. This work is
second in the cross-plan order owned by
[the roadmap](00-remaining-work.md). The product owner approved the fictional
scenarios and implementation sequence below before implementation began.

A data manager must be able to migrate a hierarchy and the records that use it
without confusing identically named branches, treating a legitimate root as
incomplete, or loading children before their parents are safely available.
When something fails, Impodo must explain the original problem, identify its
full impact, and guide correction and recovery without losing accepted data
or duplicating records.

The remaining work qualifies the complete journey and fixes gaps exposed by
the scenarios. It builds on the existing preparation, comparison, execution,
Recipe, recovery, and issue-projection owners.

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

## Implementation sequence and exit criteria

1. **Reconcile the baseline.** Record implemented foundations and current route
   limits. Preserve concurrent checkout work and inspect overlapping files
   again before editing.
2. **Build deterministic fixtures.** Reuse existing source, hierarchy,
   preparation, Recipe, comparison, and execution helpers. Define expected
   records and failures independently of the output being tested.
3. **Qualify the complete journey.** Exercise accepted files, generated and
   supplied parent tables, saved mapping, preparation, comparison, frozen load
   order, execution, reconciliation, and Recipe reuse. Fix exposed behaviour
   at its existing owner and preserve historical evidence compatibility.
4. **Complete correction guidance.** Verify authoritative root-cause links,
   distinct affected-record totals, overlapping causes, paging, authorization,
   and stale-evidence rejection. Extend the existing projection only where
   the scenarios demonstrate a gap.
5. **Qualify transport and recovery.** Exercise ordinary same-major Odoo 19 and
   final Odoo 20 paths. Keep deterministic transport fixtures separate from
   live disposable local and remote target results. Odoo 20 Production remains
   disabled. No customer Production load is part of this qualification.
6. **Measure size and failure volume.** Increase depth, fan-out, repeated names,
   and error density within each route's supported envelope. Record request
   counts, peak memory, connection and schema-check counts, retries, hashes,
   and retained outcomes. Use existing benchmark owners and opt-in large
   probes; do not infer live latency from fixture timings.
7. **Close documentation and delivery evidence.** After verifying behaviour,
   update its owning current documentation and capture any changed browser
   decision with authenticated fictional data. Run focused tests, Python
   quality, documentation checks, and the complete repository gate before
   delivery. Any failure blocks delivery until corrected or explicitly waived
   by the human for that named guardrail.

Completion requires the expected business records, actionable failure review,
and safe recovery across the approved qualification matrix. Keep this plan
open while live transport or supported-platform evidence is missing. Record
omitted environments explicitly instead of calling simulated results live
acceptance.

## Qualification record

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

### Qualification still open

Docker is installed on this macOS host, but its daemon was unavailable. No
disposable live Odoo 19 or Odoo 20 database or approved remote target was
allocated for this work. Live local and remote same-major acceptance therefore
remains omitted, including real model defaults, create hooks, and target access
rules. The existing 100,000-row relationship probe remains separately opt-in
and was not run. Shared-server and other-host release evidence is also not
provided by these local tests. Keep this plan until that external qualification
is recorded; Odoo 20 Production remains disabled.
