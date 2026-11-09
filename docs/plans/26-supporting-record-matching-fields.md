---
audience: developer
kind: plan
status: proposed
---

# Choose the Odoo field used to match a linked record

A data manager importing bills of materials may have the finished product’s
code in `ItemID`. In **Match data → Connect linked values**, they should be
able to tell Impodo to find the existing Product Template by **Internal
Reference**. A file with a customer reference should be able to use the same
control to find an existing Contact by **Reference**, including a custom
field that the destination exposes as a usable identifier.

The source column supplies the lookup value. The linked record’s matching
field tells Impodo where to look for it in Odoo. The BoM’s own matching rule
still identifies the BoM. Impodo uses the checking user’s existing Odoo
access for the lookup. The related model stays outside the migration’s write
scope.

## Observed behavior

In a BoM-only file workspace, the saved comparison used `product.template.name`
for the `mrp.bom.product_tmpl_id` relationship. Product codes therefore
appeared as misses against **Name**, even when the operator could see those
codes under **Internal Reference**. The saved comparison proves which field
Impodo queried. A fresh lookup on the intended field is required to establish
which rows resolve uniquely for the checking user.

The editor can already use a confirmed key on a model whose fields are in
the primary capture. Outside that scope, it offers a limited set of reviewed
candidates and generic name-based choices. Final review already accepts an
arbitrary supported field when matching has captured the exact target-bound
evidence. The missing step is how a data manager chooses and verifies that
field on a linked model.

## Proposed workflow

The first slice covers file-source relationships that link each source row to
one existing Odoo record. Use the same workflow for standard and custom models.

1. Keep **A value from the source** and **Only existing Odoo records** as the
   existing provider choices. In the same relationship editor, let the data
   manager select the source column and the field on the linked Odoo model.
   Preserve each saved matching rule when the editor reopens.
2. Load eligible field choices on request for the model reached by that exact
   captured relationship. Show the Odoo labels and disambiguate repeated labels
   with the technical field name. Start with exact matching on one supported,
   searchable scalar field. Keep existing compound and scoped rules available.
3. Save the field with the relationship rule. **Check matches** checks the
   distinct included source values against the chosen field and company
   context. Report a unique match, an ambiguous match, or a completed lookup
   with no match. Report incomplete evidence separately and keep the operator’s
   edits available after a failed check.
4. When a row fails, show the source column, the matching field Impodo
   actually queried, and the result. The correction action opens that linked
   field in **Match data**. A fresh comparison applies the saved rule and
   reports any remaining problems independently.

## Implementation boundaries

- Derive the linked model from the captured parent relationship. Require the
  existing workspace authorization and current Odoo read access before
  returning field choices. Keep metadata discovery explicit, scoped to that
  model, bounded, and separate from reading matching record values.
- Extend the shared [reference policy](../../src/impodo/domain/workspace/reference_keys.py)
  to govern the selected field using verified Odoo metadata for the connected
  major. Reject changed, inaccessible, or unsupported field contracts before
  querying values. Keep the existing approved name and reviewed key paths.
- Reuse `RelationshipResolver` for the saved rule and `SupportingLookupSnapshot`
  for checked evidence. Reuse [the mapping lookup planner](../../src/impodo/application/workspace/mapping/supporting_values.py)
  and [the bounded target readers](../../src/impodo/web/composition/target_readers.py)
  to query exact source values in batches. Publish only evidence bound to the
  field, target, current checking identity, company, and policy. A partial
  response cannot establish that a value is missing or unique.
- Keep the relationship rule portable in a Recipe. During application, verify
  that the destination exposes the required field and obtain fresh evidence
  under the run’s own access context. Preserve earlier Recipe versions and
  their saved field choices.

## Acceptance evidence

- Reproduce the mismatch with fictional product codes that differ from every
  Product Template name. Choose **Internal Reference**, save, reopen, check,
  prepare, and compare. Verify that the BoM links to the expected existing
  template and the Product Template stays outside the write scope.
- Repeat the same journey on a separate custom model with an arbitrary field
  such as `x_asset_code`. Include a saved Name rule and prove it still uses Name
  after the update and on reopening.
- Cover an exact match, a missing value, duplicates, an incomplete response,
  a denied or changed field, a forged relationship, and a company change.
  Assert the requested fields, exact predicates, row limits, and separation
  between targeted results and complete catalogues.
- Exercise authoring and Recipe runs for Odoo 19 and final Odoo 20 through
  local and remote access. Verify that database ownership ends before Odoo
  I/O and that page rendering only reads local evidence. Run the focused
  workflow tests and the complete repository gate before delivery.
