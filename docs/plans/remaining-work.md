# Impodo remaining work

## Status and authority

**Status:** Active roadmap, updated 2026-08-19.

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

Recipe Phases R0 through R4 are complete: the active Recipe-first aggregate,
DataVersion, TargetBinding, qualification, cutover, credential-rotation,
intent, recovery, and bound contracts are frozen with deterministic fixtures
and executable tests. The Recipe root, independent Recipe/DataVersion lineage,
protected persistence, migration ledgers, recovery intents, workspace seals,
and compatibility resolution for current project routes are implemented.
Recipe-native creation, a nonduplicating RecipeDraft readiness projection,
portable compilation, immutable publication, and Recipe/DataVersion history
are implemented. Current remote Test TargetBindings, separately supplied
credential generations, same-ish source binding, focused drift review, fresh
preparation/governance/mapping materialization, mapping-bound quality seeds,
and protected application evidence are implemented. Exact current Test
preparation, quality, comparison, execution, read-back, reconciliation,
protected qualification, later-revision invalidation, and explicit rollout
candidate selection are implemented. Phase R5 — apply the selected revision to
latest data and a separate Production target — is current.
Matching remains the existing workspace experience unless Recipe-specific
context requires a small change.

The current preparation limits remain:

- 100,000 physical rows only for exact-snapshot, single-dataset direct mappings
  compiled entirely to the verified native-columnar route;
- 50,000 physical rows for current direct Python-fallback or relationship
  routes; and
- 25,000 physical rows for current derived or materialized routes.

This roadmap decision does not raise, remove, or reinterpret those limits.

## Sole product priority — implement Recipe test-to-production reuse

**Priority decision, 2026-08-19:** Product ownership made the
[Recipe-first test-to-production implementation
plan](reusable-recipes-and-data-versions-implementation-plan.md) the only
current product-delivery focus. All competing feature, scale, certification,
gateway, hosted, and general production-hardening tracks are deferred until the
Recipe definition of done passes.

The required outcome is:

> A data manager authors and fine-tunes immutable Recipe revisions with
> representative data against a remote Test Odoo server, qualifies one exact
> revision from successful execution and reconciliation, then applies that
> qualified revision on rollout day to the latest same-format-kind data and a
> different compatible Production Odoo server using current independently
> supplied API credentials.

The Recipe work owns the following sequence. Steps 1 through 6 completed on
2026-08-19; step 7 is current:

1. rebase the frozen architecture around Recipe as aggregate root — completed;
2. add Recipe/DataVersion lineage, protected storage, and recovery — completed;
3. create, author, and publish a composite Customer Recipe — completed;
4. bind current remote Test Odoo server and credential evidence — completed;
5. apply same-ish data and review only drift — completed;
6. execute, reconcile, qualify, and select a cutover candidate — completed;
7. run that exact revision with the latest data on a different Production Odoo
   server and different API keys — current;
8. prove credential rotation and remote failure invalidation; and
9. qualify Customers, Product/BOM, and parameterized stock-level Recipe shapes
   within their currently supported limits.

Maintenance, security fixes, data-loss prevention, dependency compatibility,
and regressions blocking this Recipe path remain in scope. They do not reopen a
deferred product track.

## Deferred tracks

The following sections are retained so their existing plans, evidence, and
anchors remain discoverable. They are not current implementation priorities.

## 1. Qualify related and mixed preparation at 100,000 rows

**Status:** Deferred until the Recipe definition of done passes.

The existing
[high-volume transformation architecture implementation
plan](transformation-scale-architecture-plan.md), measurements, fixtures, and
acceptance evidence remain valid historical and future inputs. Do not raise the
current relationship/derived limits or resume generalized scale work while the
Recipe plan is active.

Recipe acceptance may use representative Customer, Product/BOM, and stock-level
volumes only within the route limits already supported. A concrete Recipe
blocker may justify the narrowest measured performance fix required for that
acceptance path; it does not reopen the general 100,000-row objective.

## 2. Add optional clean-package certification

**Status:** Deferred until the Recipe definition of done passes.

Formal organization-specific certification remains a conditional future track.
The active Recipe plan implements exact Test qualification and Production
fresh-evidence boundaries only. It does not claim a general clean-package
certificate or reuse Test qualification as Production approval.

## 3. Complete general remote acceptance and production readiness

**Status:** Deferred except for the exact remote Test-to-Production behavior
required by the active Recipe plan.

The Recipe vertical slice includes current remote server binding, API-key
generation changes, principal/permission capture, comparison, explicit write
authority, unknown-write recovery, read-back, and reconciliation. Broader
production matrices, representative-customer rollout programs, organization
assurance levels, and business actions remain deferred.

The retained
[remote Odoo acceptance runbook](../developer/runbooks/remote-odoo-acceptance.md)
continues to govern existing opt-in acceptance behavior.

## 4. Complete guarded Odoo-source updates

**Status:** Deferred until the Recipe definition of done passes.

The [Odoo source import and round-trip update implementation
plan](odoo-source-import-plan.md) retains its completed capture and comparison
evidence and its future guarded-update design. No later Odoo-source phase may
displace the Recipe work.

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
conditional on a hosted deployment requirement. The local contained workspace
remains the implementation target for the active Recipe plan.

## Reopening another track

No deferred track becomes active automatically. After the Recipe definition of
done passes, product ownership must make a new explicit priority decision in
this file. That decision must name the next track, its accepted prerequisites,
and any interaction with current Recipe behavior and evidence.
