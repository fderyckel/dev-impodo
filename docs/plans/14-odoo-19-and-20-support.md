---
audience: developer
kind: plan
status: active
---

# Odoo 19 and Odoo 20 qualification follow-up

## Decision summary

**Status:** Odoo 19 and final Odoo 20 are supported, including qualified-plan
Production. The maintainer confirmed Odoo 20 production use on 2026-10-31.
The previous separate Odoo 20 Production promotion requirement is superseded.
This plan tracks additional acceptance coverage, rather than a switch needed
to enable Odoo 20.

Current behavior belongs to the [Odoo data workflow](../developer/workflow/02-odoo-data.md),
[loading workflow](../developer/workflow/06-load-into-odoo.md), and
[Production lifecycle contract](../developer/contracts/production-run-lifecycle.md).
The original staged proposal and its dated evidence remain in Git history.

## Supported boundaries

Both majors use the same Impodo application, JSON-2 transport, workflow,
authorization, execution journal, and reconciliation engine. Final Odoo 20
supports connection, schema capture, bounded source capture, comparison,
Recipe authoring, loading, recovery, verification, and qualified-plan
Production. There is no separate Odoo 20 activation switch.

Recipes and transfers retain the same-major rule: 19 to 19 or 20 to 20.
An Odoo 19 Recipe or Test qualification does not authorize Odoo 20 Production,
and the reverse direction is also blocked. Production requires a different
live target of the qualified Test's major version, a fresh complete delivery,
separate read and write credentials, and current comparison and approval
evidence. Odoo-source Production remains outside the supported lifecycle for
both majors. Cross-major transfer, automatic Recipe conversion, and
whole-database Odoo upgrades remain excluded.

The Odoo 20 policy accepts final `20.0` builds, including the Enterprise
suffix. Prereleases, SaaS series, unknown or conflicting versions, later minor
series, and unsupported majors remain blocked. Live schema and installed
modules determine the usable fields and operations. Support does not imply
that every custom module has been exercised.

## Evidence and its limits

- The 2026-09-23 read qualification covered final Community and Enterprise
  deployments, distinct source and reference policies, and native Odoo 20
  Unit of Measure and BOM fields.
- The 2026-10-08 Contact canaries passed on remote Enterprise and local
  Community. Each file-to-Odoo route canary journalled three creates and
  verified all three through reconciliation.
- The 2026-10-31 authenticated remote check reported `20.0+e`. Impodo passed
  read identity, complete metadata capture for six standard models, and a
  bounded two-row Country reference read without limitations. It performed
  no business-record writes.
- The isolated Production lifecycle fixture covers both Odoo 19 and Odoo 20.
  It uses actual Recipe compilation, Test execution and qualification, a fresh
  Production delivery, activation, interrupted setup, restart, controlled
  execution, and reconciliation. Odoo metadata, identities, and record
  transport are fictional. This is separate from live-server acceptance.

The live deployment check does not establish throughput or a live Production
cutover result. Additional coverage below strengthens release evidence; it
does not reinstate a version-specific product restriction.

## Remaining qualification work

Run the real-server matrix on disposable databases, separately for each
supported major and deployment type. Record the exact reported build,
Community or Enterprise edition, installed modules, scenario, result, and
request counts. Keep credentials, host names, and business values out of
portable reports.

1. Extend representative live acceptance beyond Contacts to Product
   categories, Products and generated variants, BOMs, relationships, and
   representative custom fields.
2. Exercise lost responses, interrupted writes, read-back, repeat unchanged
   previews, and completed-load correction without duplicate creates.
3. Complete a live Recipe publication, Integrated Test qualification, and
   Production activation journey on two distinct disposable Odoo 20 targets.
4. Exercise a live 20 to 20 transfer and confirm both cross-major directions
   stop before writing.
5. Extend live multi-company, permission restriction, and Enterprise-module
   evidence where those deployment features are used.
6. Repeat representative timing and peak-memory measurements per major with
   the same workload. Keep fixture measurements separate from live timings.

Reuse [remote acceptance](../developer/runbooks/remote-odoo-acceptance.md)
and [scenario qualification](../developer/runbooks/scenario-qualification.md).
The read-only qualification runners must remain read-only even though the
product policy also enables writes and Production. Their scope does not
define the supported product operation set.

## Regression requirements

Keep both majors in the connector and workflow regression suites. Preserve
Odoo 19 policy hashes, stored identities, and existing Recipe evidence when
changing version-specific policies. Test unsupported versions, conflicting
version evidence, Recipe mismatches, mixed-major plans, target reuse,
credential rotation, in-place server upgrades, and recovery after an unknown
write outcome.

Run focused behavioral tests and the complete repository gate before delivery.
Update current documentation only alongside verified implementation changes.
