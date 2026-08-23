# Recipe Phase R6 credential rotation implementation report

**Historical evidence:** This report records the superseded Recipe-first
implementation as it existed on 2026-08-19. ADR-014 and Migration Project
Phases M0 through M7 define the current architecture.

## Outcome

R6 closes the credential-dependent evidence boundary for the Test-to-Production
Recipe workflow. A Production read API key can no longer be rotated while old
schema, principal, permission, comparison, or execution readiness silently
remains usable. The Recipe revision and source evidence are unaffected, but the
data manager must refresh the Odoo schema and compare again under the new key.

The familiar final-review, load-confirmation, and outcome screens remain in
place. A stale credential now makes the existing load review unavailable and
shows the focused refresh-and-compare recovery action.

## Read generation and comparison boundary

Every stored read or write key replacement already creates a random,
secret-independent binding generation. R6 now consumes that binding consistently:

- remote comparison requires the current read generation to match the captured
  schema generation exactly;
- comparison re-probes the full captured model scope and requires the exact
  target, principal, permission, context, and readable-model evidence;
- the execution snapshot contract is now version 3 and carries those safe read
  bindings with the exact compared target evidence; and
- the current load preview compares the stored read generation with the
  snapshot generation, so a rotation immediately removes load readiness.

Changing only the API key while retaining the same Odoo user is intentionally
still a new generation. Principal equality is not accepted as a substitute for
refreshing credential-bound target evidence.

## Load and recovery boundary

Immediately before a remote load, Impodo re-probes the current read key against
the exact snapshot model scope. The generation, target, principal, permissions,
context, and readable models must all still match before an execution journal is
started or a write is sent. The separate write key is then freshly probed against
the exact read-back and writable model scope as before.

Write authority remains application- and target-specific. It is not part of a
Recipe and comparison never grants it. A write key may therefore legitimately
be introduced or rotated between comparison and load after its fresh probe.

Recovery after an uncertain or unverifiable write also permits a rotated write
key, because an expired key must not make reconciliation impossible. The new key
must freshly prove the exact execution target, principal, permission, context,
read-back scope, and write scope. Reconciliation contract version 2 records the
safe verification-key generation and freshly probed principal, permission, and
context hashes separately from the original execution-key evidence. It never
records either secret.

Unknown writes retain the existing stop-and-reconcile behavior: the writer
stops after the first uncertain response, journals every affected and unattempted
row, and never retries the write. Reconciliation re-matches uncertain records by
the reviewed business key and marks a future attempt safe only when absence was
proved.

## Remote failure qualification

| Injected condition | Qualified behavior |
| --- | --- |
| Expired or rejected read key | Authentication failure is redacted; no comparison or load proceeds. |
| Read or write ACL change | Fresh probe evidence differs or access is rejected before the dependent operation. |
| Connection failure | No comparison evidence or execution journal is created before contact; a lost write response is journalled as unknown. |
| Schema drift | Captured-schema and projection validation fail closed during comparison; incompatible fields cannot produce a current load snapshot. |
| Missing target reference | The row remains blocked or ambiguous and cannot enter a loadable snapshot. |
| Unknown write response | The journal stops further writes and requires read-back; there is no blind retry. |
| Read-back failure | The completed/unknown execution journal remains durable, no false reconciliation result is published, and recovery can use a freshly probed key. |

Connector status errors remain bounded and omit response bodies. Network errors
omit exception details. Odoo import-response messages are now replaced with a
fixed rejection message because server-provided detail can contain credentials
or business data. Credential envelopes, snapshots, journals, reconciliation
results, audit events, and rendered pages contain only safe hashes and labels.

## UI continuity

No new parallel credential screen or execution flow was introduced. The
existing target setup continues to own the read key, schema refresh remains in
the existing Odoo-data step, load confirmation continues to request the separate
write key, and the outcome screen continues to own reconciliation. Only current
state and recovery copy changes when credential evidence is stale.

## Verification

Focused coverage proves:

- same-target read-key rotation is freshly probed but cannot reuse the old
  schema or comparison;
- an ACL change blocks comparison before target records are read;
- the load preview becomes unavailable as soon as the read generation changes;
- load-time ACL drift stops before journalling or target writes;
- a rotated recovery key succeeds only with the exact original write identity
  and records its new safe generation;
- Odoo import details containing a secret marker are not exposed; and
- existing uncertain-create and uncertain-update paths never retry blindly.

Final verification passed:

- 694 repository tests passed, with 13 environment-dependent tests skipped;
- bytecode compilation passed for source and tests;
- documentation quality and code-documentation inventory checks passed; and
- the working-tree diff passed whitespace validation.
