# Impodo documentation

Choose the path that matches your work:

- [User documentation](user/README.md) guides data managers through the
  current browser workflow in business language.
- [Developer documentation](developer/README.md) maps the same workflow to
  routes, services, evidence, invalidation rules, performance boundaries, and
  tests.

Accepted decisions and contracts are normative. Architecture describes the
current composition and boundaries; audience pages explain how to use it; testing
records evidence; the remaining-work plan and product vision describe future
delivery. Where an example conflicts with a **MUST**, **MUST NOT**, **SHOULD**,
or **MAY** rule, the normative rule wins.

## Workflow terminology

**Recipe setup** creates the Recipe and authoring DataVersion workspace before
the six registered data-version stages:

1. **Source data**
2. **Odoo data**
3. **Match data**
4. **Prepare data**
5. **Final review**
6. **Load into Odoo**

An Odoo-source project presents the first two responsibilities as **Odoo source
data** and **Freeze Odoo records**. The architectural product vision uses
**Stages A–K** to describe the wider business lifecycle; those letters are not
browser navigation labels.

The machine-readable [workflow registry](workflow.yml) owns documentation,
route, template, code-symbol, contract, and focused-test coverage for each
stage. The [documentation style guide](style-guide.md) defines the audience and
voice rules. The project-local
[Impodo documentation skill](../.agents/skills/impodo-documentation/SKILL.md)
applies those rules when documentation is created or rewritten.

## Architecture

- [Architecture overview](architecture/overview.md) — current system context,
  browser and preflight boundaries, component layers, evidence flow,
  performance invariants, and deployment seams.
- [Python code map](architecture/python-code-map.md) — navigation from browser
  and CLI entry points through services, domain behavior, repositories, and
  migration evidence.
- [Security and infrastructure](architecture/security-and-infrastructure.md) — factual
  overview of the local architecture, implemented controls, infrastructure
  requirements, data handling, verification evidence, and current limitations.
- [End-to-end migration product vision](product-vision.md) — complete product
  workflow, mapping architecture, staging, relation handling, executor
  boundary, edge cases, and roadmap.
- [Architecture decisions](decisions/README.md) — accepted decisions that
  constrain implementation.

## Process models

- [Current Impodo BPMN models](bpmn/README.md) — BPMN 2.0 overview and detailed
  diagrams for Recipe/data-version setup and all six implemented browser responsibilities,
  including the current file-source, Odoo-source, and disposable-target
  boundaries.

## Plans

- [Impodo remaining work](plans/remaining-work.md) — the authoritative
  forward-looking roadmap. Migration Project ownership, optional reusable
  Recipes, Project-owned data packages, and integrated multi-Recipe cutover
  are the current product-delivery focus.
- [Migration projects and multi-Recipe cutover implementation plan](plans/migration-projects-and-multi-recipe-cutover-implementation-plan.md)
  — accepted target architecture and active plan for replacing the current
  Project-as-Recipe model without retaining compatibility shells, aliases, or
  old storage readers. Phases M0 and M1 are complete; the browser cutover has
  not started.
- [Migration Projects Phase M0 contracts](plans/migration-projects-phase-m0-contracts.md)
  — completed architecture-only contracts and executable fixtures for Project
  ownership, optional and multiple Recipes, Project-owned data packages,
  integrated runs, CutoverPlans, and exact qualification.
- [Migration Projects Phase M1 persistence foundation](plans/migration-projects-phase-m1-foundation.md)
  — clean Project, DataVersion, run, and workspace roots, exact new DuckDB
  generations, bounded projections, restart-safe intents, old-storage
  rejection, and recoverable development reset. These services are not yet
  composed into the browser.
- [Selection value providers and conditional rules implementation plan](plans/selection-value-providers-and-rules-implementation-plan.md)
  — approved design for separating Odoo choices from source values, preserving
  fixed choice mappings, and adding a governed multi-column rule provider for
  Odoo Selection fields. The behavior is not yet implemented.
- [Odoo source import and round-trip update implementation plan](plans/odoo-source-import-plan.md)
  — scoped proposal for selecting existing Odoo 19 records as immutable
  Impodo source data, transforming them, and applying guarded updates back to
  the same database.
- [High-volume transformation architecture implementation plan](plans/transformation-scale-architecture-plan.md)
  — weighted comparison of four scale architectures, with a phased proposal
  for reducing transformation CPU and memory, extending bounded preparation to
  related Products and BOMs, and retaining governed audit evidence.
- [Reusable recipes and data versions implementation plan](plans/reusable-recipes-and-data-versions-implementation-plan.md)
  — completed historical plan for the currently implemented Recipe-first
  vertical slice. ADR-014 and the Migration Project plan supersede it as
  forward-looking architecture authority.
- [Recipe-first Phase R0 contracts](plans/reusable-recipes-phase-r0-contracts.md)
  — historical frozen contracts for the currently implemented Recipe-first
  slice. Current runtime contracts remain authoritative until each replacement
  implementation gate passes.
- [Recipe-first Phase R1 implementation report](reports/reusable-recipes-phase-r1-persistence-2026-08-19.md)
  — completed Recipe/DataVersion registry lineage, protected payload storage,
  workspace linkage and sealing, compatibility resolution, and deterministic
  intent-recovery evidence.
- [Recipe-first Phase R2 implementation report](reports/reusable-recipes-phase-r2-authoring-2026-08-19.md)
  — completed Recipe-native creation, current-evidence draft projection,
  portable compilation, immutable publication, and revision-history evidence.
- [Recipe-first Phase R3 implementation report](reports/reusable-recipes-phase-r3-test-application-2026-08-19.md)
  — completed remote Test TargetBinding, same-ish source application, focused
  drift, fresh mapping/preparation/quality seeds, and protected evidence.

## Guides, runbooks, and quality

Data-manager guides live under [user documentation](user/README.md). Technical
setup, CLI, release, and acceptance procedures live under
[developer documentation](developer/README.md).

- [End-to-end local-browser tutorial](user/tutorials/end-to-end-training.md)
  — one complete fictional migration across the current browser workflow.
- [Windows installation](user/installation/windows.md) — step-by-step GitHub
  evaluation and accepted internal release routes.
- [Local Odoo guide](user/guides/local-odoo.md) — local target readiness,
  ownership-aware start and stop behavior, and troubleshooting.
- [Local Odoo technical runbook](developer/runbooks/local-odoo.md) — process
  ownership, restart safeguards, and technical troubleshooting.
- [Profile authoring](developer/cli/profile-authoring.md) — strict YAML datasets,
  business identities, fields, relationships, and validation workflow.
- [Preflight CLI runbook](developer/cli/preflight.md) — safe profile-driven snapshot
  and offline classification sequence, evidence rules, and exit behavior.
- [Windows developer setup](developer/setup/windows.md)
  — IT provisioning, installation boundaries, Odoo access, and verification.
- [Internal development and release runbook](developer/runbooks/internal-release.md) —
  development setup, authoritative dependency locking, promotion, evidence,
  and installation of an accepted internal bundle.
- [Remote Odoo 19 acceptance](developer/runbooks/remote-odoo-acceptance.md) —
  opt-in sanitized remote load, read-back, repeat-preview, and throughput
  evidence against a disposable on-premises database.
- [Related-table authoring](user/guides/related-tables.md) — the
  generic browser reference for extracting reusable records or separating
  repeated parent/child rows without editing frozen source data.
- [Developer examples and edge cases](developer/reference/examples-and-edge-cases.md)
  — CLI runs, profile patterns, prepared records, classifications, issue codes,
  and connector cases.
- [Acceptance and test strategy](testing/acceptance.md) — test layers, golden
  slice, determinism checks, and acceptance traceability.
- [Design QA evidence](testing/design-qa.md) — point-in-time visual fidelity
  findings, completed checks, and blocked browser-verification evidence.
- [Glossary](glossary.md) — canonical project terminology.

## Documentation maintenance

Keep one active authority for each concept. When a contract changes, update
its implementation, fixtures, examples, generated artifacts, tests, and links
together. Label proposals and historical delivery documents explicitly; use
Git history instead of retaining stale architecture summaries in the active
documentation tree.

For workflow changes, update the paired user and developer stage pages and the
workflow registry. Also update the owning module/class/method docstrings and
the [Python code map](architecture/python-code-map.md). Run:

```console
python scripts/documentation_quality.py --check --report
python scripts/code_documentation_inventory.py --check
python scripts/code_documentation_inventory.py --missing
python -m unittest tests.test_documentation_quality tests.test_code_documentation
```

The workflow and module checks are blocking. The public-symbol list and Vale
style rules are advisory and require semantic review rather than percentage or
readability-score targets. Run `vale docs` to check every documentation lane.
Vale accepts Impodo and Odoo terminology, but it asks writers to explain
implementation terms before using them in user documentation. Treat each Vale
alert as a review prompt: correct unclear prose, preserve exact example data and
identifiers, and improve the rule when it produces a repeatable false positive.
