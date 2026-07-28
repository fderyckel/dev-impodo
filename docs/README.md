# Impodo documentation

This documentation is the design authority for the read-only Odoo preflight
milestone. Where an example and a normative rule disagree, text marked
**MUST**, **MUST NOT**, **SHOULD**, or **MAY** is authoritative.

## Architecture

- [Architecture review](architecture-review.md) — current-state evidence,
  fitness assessment, risks, and readiness gates.
- [Read-only preflight architecture](architecture/read-only-preflight.md) —
  boundaries, components, data flow, invariants, comparison semantics, and
  deployment model.
- [Architecture decisions](decisions/README.md) — accepted decisions that
  constrain implementation.
- [Implementation plan](implementation-plan.md) — package layout, sequence,
  deliverables, and definition of done.

## Contracts

- [Profile contract v2](contracts/profile-v2.md) — how a profile maps,
  types, identifies, resolves, and compares data.
- [Prepared record contract v1](contracts/prepared-record-v1.md) — the
  environment-independent boundary after source validation.
- [Snapshot contracts v1](contracts/snapshots-v1.md) — environment
  fingerprint, model metadata, and target-record catalogs.
- [Preflight result contract v1](contracts/preflight-result-v1.md) —
  classifications, field differences, reference evidence, and portable
  manifest rules.
- [Read connector contract](contracts/read-connector.md) — the intentionally
  narrow interface implemented by fixtures and live Odoo access.

## Operations and quality

- [CLI and operating model](operations/cli.md) — commands, artifact flow,
  exit behavior, secrets, and runbook.
- [Examples and edge cases](examples-and-edge-cases.md) — copy-paste runs,
  profile patterns, expected outcomes, failure cases, and current limitations.
- [Acceptance and test strategy](testing/acceptance.md) — test layers, golden
  slice, determinism checks, and acceptance traceability.
- [Glossary](glossary.md) — canonical project terminology.

## Versioning

The profile, prepared-record, snapshot, and result contracts each carry an
independent integer `contract_version`. Backward-compatible additions do not
change the integer. A change in meaning, a removed field, or a newly required
field requires a new contract version and an explicit migration.

All source, fixtures, examples, generated review packages, and documentation
belong under `/Users/francois/dev-impodo`. No commit, push, publication, or
deployment is performed by this milestone.
