---
audience: developer
document_type: plan
status: implemented
---

# Keep Odoo load errors actionable for the operator

## Implemented result

When Odoo rejects a record, the data manager should be able to identify the
affected source row, understand Odoo's explanation, inspect the value that was
actually submitted, and reach the appropriate correction action. A generic
reason code is useful for support grouping but cannot replace that explanation.

The private receipt, authorized review, and bounded row navigation are
implemented in the
[execution contract](../developer/contracts/execution-and-reconciliation.md#private-rejection-explanations).
Existing explanations that were discarded cannot be recovered from a hash or
reason code. They must remain explicitly unavailable.

## Original loss of information

[`import_rejection_diagnostic`](../../src/impodo/adapters/odoo/write_diagnostics.py)
inspects bounded Odoo messages, classifies their wording, and retains only
validated technical fields, a batch range, counts, and a fingerprint. The
[`OdooWriteDiagnostic`](../../src/impodo/domain/execution/write_diagnostics.py)
summary previously supplied the operator's only saved error. When Odoo put the
explanation only in message text, the operator lost the detail needed to correct
the row. The separate private receipt now preserves that explanation.

The contract intentionally keeps arbitrary server text and source values out
of routine diagnostics and the value-free execution row projection. Preserve
that boundary. Add a separately authorized private explanation rather than
putting raw server messages in `safe_error`, logs, or diagnostic ZIPs.

## What the operator should see

The following is a fictional example of the proposed presentation, not an
explanation of an observed customer rejection:

```text
Products · source row 12 — Not loaded
Field: Sales price (list_price)
Value sent to Odoo: twelve
Odoo's explanation: Could not convert 'twelve' to a number.
Next action: Review this row and its matching rule.
```

Show the field and submitted value only when the response and reviewed mapping
establish that attribution. Read the submitted value from the frozen request
evidence after transformations, normalization, blank handling, and relationship
resolution. Do not substitute a current source cell or an edited mapping for
the value sent by the failed attempt.

If Odoo reports a record-level constraint without naming a field, show its
explanation and say **Odoo did not identify a field**. Offer the reviewed fields
sent in that request as context, clearly separated from identified causes.
Do not guess a field from the model, a word such as "invalid", or a common
failure on another record.

Lead with the dataset and source row. Keep batch-relative positions under
Support details. Attribute an error to a source row only when the captured
batch membership and Odoo's reported range establish the association. A
rejected batch does not prove that each member contains a bad value. A source
record expanded into several prepared rows needs its precise source trace.

The correction action should open the existing row or mapping decision and
preserve its work. If correction belongs in Odoo, explain the required target
change. Preserve accepted records and require the existing preparation,
comparison, approval, and recovery checks before another write.

## Store private explanations separately from support diagnostics

Extend the owning execution repository with a bounded private error receipt.
Bind it to the workspace, execution run, transport attempt, target model,
reviewed snapshot, exact batch membership, and any validated field and row
associations. Keep the original meaning of Odoo's user-facing validation
explanation instead of replacing it with a classifier's paraphrase.

Extract only the verified user-message portion of each supported Odoo response.
Never store an entire HTTP body, headers, traceback, SQL diagnostic, credential,
or internal server path as operator detail. Escape text when rendering it.
Apply the same workspace and field-value authorization as the submitted data;
protected values stay protected even if Odoo echoes them. Preserve correctable
business values for authorized operators rather than applying the routine ZIP's
value-free policy to their private view. If an explanation cannot be separated
safely from protected content, state that limitation explicitly.

Start with the adapter's existing twenty-message and 8,192-character inspection
bounds, then enforce explicit byte and storage limits. Record truncation and
capture failure rather than presenting partial detail as complete. Persist
the outcome and its private-detail reference through the existing repository's
short transactional ownership. A detail-capture failure must preserve the
known Odoo outcome and accepted receipts; it must not trigger another write.

Choose and version the private storage contract explicitly during
implementation. Cover every active storage route. Do not append business text
to `ExecutionRowAttempt`, change immutable execution hashes, introduce a second
load tracker, or add a parallel reader for a retired format. Historical loads
without private detail retain their existing evidence and report its absence.

Expose private detail through the authorized load-outcome service and view.
It must survive refresh and reopening. Keep it out of general logs, progress
payloads, diagnostic settings, routine ZIPs, and public summaries. Existing
support exports may report bounded availability and attribution flags, such as
detail captured, detail truncated, field unspecified, or detail unavailable,
without exporting the private explanation or submitted value. Do not describe
an unexplained generic rejection as having complete cause coverage.

## Implementation owners

1. Extend the adapter's error result to carry the bounded private explanation
   separately from its existing safe diagnostic. Verify structured field paths
   and message shapes for Odoo 19 and Odoo 20.
2. Let the application service bind and persist that explanation through the
   execution repository. The presenter must not infer a cause or read a store
   directly. Keep local owners closed before Odoo I/O.
3. Add the authorized outcome detail and correction links to the existing row
   review. Preserve error detail for isolated child batches and relationship
   completion failures, not just the initial create call.
4. Preserve the value-free diagnostic exporter and add explicit coverage flags.
   Update the owning current contract, user and developer guides, workflow
   registry, and code map only with the verified implementation.

## Required verification

Exercise the complete response-to-saved-outcome-to-correction journey with
fictional data. Cover both Odoo majors, local and remote connections, Authoring,
Recipe runs, and affected transfer or correction entry points. Include a
second model so the repair does not depend on partner or tax-specific parsing.

- A known field rejection retains Odoo's explanation, the correct source trace,
  and the value actually sent after transformation; refresh and reopen preserve it.
- A record-level or custom rejection with no field remains actionable without
  an invented field, value attribution, or reason.
- A multi-row rejection and its isolated children retain the correct attempt
  associations. Batch positions cannot be mistaken for source row numbers.
- A malicious or oversized message is escaped, bounded, and explicitly marked
  when incomplete. Credentials, protected values, tracebacks, and private paths
  do not leak through private rendering or routine exports.
- Unauthorized users cannot fetch private details. ZIPs, general logs, progress
  payloads, and settings remain value-free even when private detail exists.
- Capture failure and a historical load without detail keep an accurate
  unavailable state while preserving accepted receipts and recovery behavior.
- An uncertain write still requires read-back. Richer wording never authorizes
  splitting, blind resend, or a change in rejection and continuation policy.

Run the focused behavioral tests, database-operation budgets, documentation
checks, and complete repository gate. Do not claim that a clearer generic
sentence fixes the defect unless the operator can identify the reported cause
and reach the actual correction decision.
