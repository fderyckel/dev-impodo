---
audience: developer
document_type: plan
status: planned
---

# Preserve source identity and ordering

The data manager must be able to import distinct source records whose ordinary
Odoo values repeat or are blank. Record matching must not require changing
those business values. The implementation applies to every supported model.

## Required behaviour

1. Match records using a stable, explicitly named source identity namespace
   and the selected complete source key. Store the identity as an Odoo External
   ID. Keep the namespace through Recipe publication, rebinding and new runs.
2. Preserve ordinary mapped values independently, including repeated sequence
   values. Apply the explicit existing blank policy and expose destination
   limitations rather than silently inventing source values.
3. Keep missing identities waiting for a reviewed creation decision. Allow
   existing records to be referenced through their existing External IDs. An
   absent binding must never silently select a business-field peer.
4. Preserve source order independently of identity. Optional generated ordering
   must be explicit, scoped and deterministic, with selected sort columns,
   blank placement and original-row tie-breaking. Explain that an import cannot
   override the destination model or view's display order.
5. Enforce complete and unique source identities, reject two source records
   claiming one target, preserve parent distinctions after joins, and retain
   rejected and unresolved decisions for review.
6. Use bounded native preparation, grouped DuckDB validation and batched exact
   Odoo reads. Close local database owners before Odoo I/O. Preserve existing
   contract hashes when the new optional policies are absent.

## Verification

Cover mapping save and reopen, Python and native preparation parity, scoped
relationships, first imports and subsequent imports, missing and conflicting
External IDs, changed file order, scalar blank values, interrupted writes,
read-back and Recipe reuse. Exercise Odoo 19 and 20, local and remote access,
custom models, historical evidence and explicit performance budgets. Run the
complete repository gate and the portable behavioural suite on the final
candidate. Keep the running application and concurrent work intact.
