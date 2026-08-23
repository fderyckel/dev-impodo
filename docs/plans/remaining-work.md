# Impodo remaining work

## Status and authority

**Status:** Active roadmap, updated 2026-08-23.

This is the authoritative cross-product roadmap in `docs/plans/`. Scoped
implementation proposals do not change its priority order unless explicitly
adopted here. Completed implementation belongs in Git, release evidence belongs
in `docs/reports/` and `docs/testing/`, and current behavior belongs in
architecture, contracts, and audience-appropriate documentation.

## Current implemented boundary

Impodo supports the reviewed disposable local and remote Odoo 19 load and
read-back path. Mapping contract v11, validation-bound categorical coverage,
split reusable control definitions/DataVersion expectations, legacy upgrade
review, and the shared application-layer source scan are implemented.

Recipe Phases R0 through R7 are complete historical evidence for the removed
Recipe-first aggregate, DataVersion, TargetBinding, qualification, cutover,
credential-rotation, intent, recovery, and bound contracts have deterministic
fixtures and executable tests. The Recipe root, independent Recipe/DataVersion lineage,
protected persistence, migration ledgers, recovery intents, workspace seals,
and compatibility resolution for current project routes are implemented.
Recipe-native creation, a nonduplicating RecipeDraft readiness projection,
portable compilation, immutable publication, and Recipe/DataVersion history
are implemented. Current remote Test TargetBindings, separately supplied
credential generations, same-ish source binding, focused drift review, fresh
preparation/governance/mapping materialization, mapping-bound quality seeds,
and protected application evidence are implemented. Exact current Test
preparation, quality, comparison, execution, read-back, reconciliation,
protected qualification, later-revision invalidation, explicit rollout
candidate selection, clean Production application, credential-rotation
invalidation, and Product, Product/BOM, and parameterized-stock qualification
are implemented.
Matching remains the existing workspace experience unless Recipe-specific
context requires a small change.

The current preparation limits remain:

- 100,000 physical rows only for exact-snapshot, single-dataset direct mappings
  compiled entirely to the verified native-columnar route;
- 50,000 physical rows for current direct Python-fallback or relationship
  routes; and
- 25,000 physical rows for current derived or materialized routes.

This roadmap decision does not raise, remove, or reinterpret those limits.

## Current product priority — Migration Projects and multi-Recipe cutover

**Priority decision, 2026-08-22:** Product ownership accepted
[ADR-014](../decisions/README.md#adr-014--migration-projects-coordinate-reusable-recipes-and-cutover-plans)
and made the [Migration projects and multi-Recipe cutover implementation
plan](migration-projects-and-multi-recipe-cutover-implementation-plan.md) the
active product-delivery focus.

The current browser uses `MigrationProject` as the business root, lets a
Project exist without a Recipe, makes DataVersion the owner of the complete
source package, and applies several exact Project-scoped Recipe revisions
through one planned Test MigrationRun. Phase M5 qualifies one exact integrated
CutoverPlan and records rollout selection separately. Phase M6 now creates a
fresh latest-data Production run with independent target and credential
authority. Phase M7 must remove the remaining Recipe-first compatibility code
and stale historical fixtures from the active implementation.

The implementation must retain the current portable Recipe compiler, fresh
Test and Production evidence, credential separation, Odoo 19 boundaries,
immutable execution evidence, and reconciliation behavior. It must remove the
Project-as-Recipe alias, Recipe-owned DataVersions and cutover pointer,
Recipe-root creation route, old schema migrations, compatibility shells, and
stale documentation before completion.

No deferred product track becomes current while this plan is active. Narrow
maintenance, security, data-loss, regression, and performance fixes required
to preserve the current workflow remain in scope.

**Phase status, 2026-08-23:** Phases M0 through M6 are complete. The [Phase M0
contracts](migration-projects-phase-m0-contracts.md) freeze the target
ownership and integrated-plan rules. The [Phase M1 persistence
foundation](migration-projects-phase-m1-foundation.md) implements the clean
Project, DataVersion, run, and workspace roots, exact new stores, bounded
registry projection, compatibility rejection, and recoverable development
reset. The [Phase M2 source-package
foundation](migration-projects-phase-m2-source-packages.md) adds immutable
DataVersion source packages, bounded workspace dataset projections, and the
mapping-source adapter without copying source state. The [Phase M3
implementation](migration-projects-phase-m3-project-authoring.md) composes the
Project-first browser, one-off authoring, and optional Recipe publication. The
[Phase M4 implementation](migration-projects-phase-m4-multi-recipe-runs.md)
adds one run-owned target and union requirement plan, isolated Recipe
applications, dependency and write-collision validation, fresh mapping drafts,
and bounded integrated status. The [Phase M5
implementation](migration-projects-phase-m5-cutover-qualification.md) adds
immutable CutoverPlan revisions, ordered exact Test qualification, dependency
write guards, protected evidence, and separate rollout selection. The [Phase
M6 implementation](migration-projects-phase-m6-production-rollout.md) adds
fresh latest-data setup, exact selected-plan activation, independent
Production read and write authority, isolated application evidence, and
restart-safe cross-store recovery. Phase M7 is next.

## Completed product priority — Recipe test-to-production reuse

**Priority decision, 2026-08-19:** Product ownership made the
[Recipe-first test-to-production implementation
plan](reusable-recipes-and-data-versions-implementation-plan.md) the only
product-delivery focus. That definition of done passed on 2026-08-19. Competing
feature, scale, certification, gateway, hosted, and general
production-hardening tracks remain deferred until product ownership explicitly
selects one below.

The required outcome is:

> A data manager authors and fine-tunes immutable Recipe revisions with
> representative data against a remote Test Odoo server, qualifies one exact
> revision from successful execution and reconciliation, then applies that
> qualified revision on rollout day to the latest same-format-kind data and a
> different compatible Production Odoo server using current independently
> supplied API credentials.

The Recipe work owned the following sequence. Steps 1 through 9 completed on
2026-08-19:

1. rebase the frozen architecture around Recipe as aggregate root — completed;
2. add Recipe/DataVersion lineage, protected storage, and recovery — completed;
3. create, author, and publish a composite Customer Recipe — completed;
4. bind current remote Test Odoo server and credential evidence — completed;
5. apply same-ish data and review only drift — completed;
6. execute, reconcile, qualify, and select a cutover candidate — completed;
7. run that exact revision with the latest data on a different Production Odoo
   server and different API keys — completed;
8. prove credential rotation and remote failure invalidation — completed; and
9. qualify Customers, Product/BOM, and parameterized stock-level Recipe shapes
   within their currently supported limits — completed.

On 2026-08-22, product ownership selected the Migration Project and
multi-Recipe cutover correction above as the next priority. Phases M0 through
M4 now implement Project-first authoring, optional Recipe publication, and
integrated Test planning. The
completed Recipe-first work remains historical implementation evidence, not
the active architecture.

Maintenance, security fixes, data-loss prevention, dependency compatibility,
and regressions blocking this Recipe path remain in scope. They do not reopen a
deferred product track.

## Deferred tracks

The following sections are retained so their existing plans, evidence, and
anchors remain discoverable. They are not current implementation priorities.

## 1. Qualify related and mixed preparation at 100,000 rows

**Status:** Deferred; the Recipe prerequisite passed, but explicit product
reopening is still required.

The existing
[high-volume transformation architecture implementation
plan](transformation-scale-architecture-plan.md), measurements, fixtures, and
acceptance evidence remain valid historical and future inputs. Do not raise the
  current relationship/derived limits or resume generalized scale work while the
  Migration Project and multi-Recipe cutover plan is active.

Recipe acceptance may use representative Customer, Product/BOM, and stock-level
volumes only within the route limits already supported. A concrete Recipe
blocker may justify the narrowest measured performance fix required for that
acceptance path; it does not reopen the general 100,000-row objective.

## 2. Add optional clean-package certification

**Status:** Deferred; the Recipe prerequisite passed, but explicit product
reopening is still required.

Formal organization-specific certification remains a conditional future track.
The current Recipe implementation provides exact Test qualification and
Production fresh-evidence boundaries only. It does not claim a general clean-package
certificate or reuse Test qualification as Production approval.

## 3. Complete general remote acceptance and production readiness

**Status:** Deferred except for the exact remote Test-to-Production behavior
already implemented and retained by the active Migration Project plan.

The Recipe vertical slice includes current remote server binding, API-key
generation changes, principal/permission capture, comparison, explicit write
authority, unknown-write recovery, read-back, and reconciliation. Broader
production matrices, representative-customer rollout programs, organization
assurance levels, and business actions remain deferred.

The retained
[remote Odoo acceptance runbook](../developer/runbooks/remote-odoo-acceptance.md)
continues to govern existing opt-in acceptance behavior.

## 4. Complete guarded Odoo-source updates

**Status:** Deferred; the Recipe prerequisite passed, but explicit product
reopening is still required.

The [Odoo source import and round-trip update implementation
plan](odoo-source-import-plan.md) retains its completed capture and comparison
evidence and its future guarded-update design. No later Odoo-source phase may
displace the active Migration Project work.

## 5. Conditional target-side gateway

**Status:** Deferred.

A signed Odoo 19 add-on, manifest-bound grants, target-side receipts, and named
business-action handlers may be reconsidered only when a proven Recipe
execution requirement cannot be met safely through the existing bounded
connector/executor contracts. No generic RPC, SQL, `sudo`, or caller-selected
method surface is permitted.

## 6. Conditional hosted composition

**Status:** Deferred.

PostgreSQL repositories, object storage, durable workers, distributed target
locks, SSO actors, centralized authorization, and managed secrets remain
conditional on a hosted deployment requirement. The local composition remains
the implementation target for the active Migration Project plan.

## Reopening another track

No deferred track becomes active automatically. After the Migration Project
and multi-Recipe definition of done passes, product ownership must make a new
explicit priority decision in this file. That decision must name the next
track, its accepted prerequisites, and any interaction with Project, Recipe,
DataVersion, run, and cutover evidence.
