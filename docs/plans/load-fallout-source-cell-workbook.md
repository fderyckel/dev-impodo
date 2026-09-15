# Add exact-record repair for known load fallout

## Status and remaining scope

**Status:** Source-cell workbooks, grouped outcome explanations, numeric
precision checks, HTML normalization, and append-only re-verification are
implemented. A governed repair action for a completed load with known fallout
remains proposed as of 2026-09-15.

The [Load into Odoo developer workflow](../developer/workflow/06-load-into-odoo.md#implementation-flow)
and [execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md#reconciliation)
own the available workbook and re-verification behavior. The
[user workflow](../user/workflow/06-load-into-odoo.md) explains those controls.
Completed delivery detail remains in Git history.

The existing successor correction workflow starts from a verified Authoring
load. This remaining proposal must define how a known-fallout result can enter
an exact-record repair without claiming that the original load was verified.

## Proposed repair flow

After reviewing a bound fallout workbook, the data manager should be able to
choose **Resolve fallout**:

1. Bind the repair to the completed execution, its immutable snapshot, the
   selected reconciliation attempt, and the exact protected target records.
2. Re-probe the same target and approved principal and read the current values
   of only the differing fields on those recorded Odoo identifiers.
3. Present the proposed field updates and fresh conflicts. Show zero creates
   and require explicit confirmation of the exact reviewed repair.
4. Journal each approved update before transport and write only those fields
   on those records. Never resend the original create plan.
5. Reconcile automatically and publish another immutable verification attempt.
   Complete the load only when the current attempt verifies all expected values
   and no outcome remains unknown.

A concurrent target edit, missing target record, stale evidence, or uncertain
write outcome must remain a visible blocker or recovery decision. Reuse the
existing authorization, protected-evidence, journal, and reconciliation owners;
settle the repair lifecycle before introducing a writer entry point.

## Evidence and verification

Preserve the original execution, all verification attempts, and their bound
workbooks. Historical workbook generation must continue to use the accepted
source, historical snapshot, and detail artifact from that exact attempt.
It must not substitute a new live read for historical evidence.

Add focused application, repository, and browser coverage for exact-ID updates,
changed target values, stale confirmation, zero creates, rejected writes, lost
responses, and read-back before retry. Keep spreadsheet formula-injection,
artifact-integrity, numeric-precision, HTML-normalization, and append-only
verification checks passing. Browser state and diagnostics must not expose
protected identifiers, business values, or credentials.

After implementation, update the paired load workflows, execution contract,
workflow registry, and authenticated repair screenshots. Record disposable
local and remote Odoo 19 qualification before retiring this plan.
