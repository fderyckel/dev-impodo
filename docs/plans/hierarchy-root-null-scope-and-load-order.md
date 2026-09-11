# Accept hierarchy roots and preserve parent-before-child load order

## Status and decision

**Status:** Core semantic correction implemented on 2026-09-11. Mapping
contract version 17, compiled-plan contract version 2/compiler version 3, and
Mapping Recipe contract version 3 now preserve the explicit hierarchy-root
null policy. This plan does not authorize an Odoo load; the affected project
must create and check new evidence before execution.

Impodo should treat an empty parent on a generated hierarchy root as an
explicit, valid target scope. It should continue to reject a missing parent on
a row that is supposed to have one.

After that correction, Impodo should use its existing row dependency scheduler
to create or resolve each parent before it sends a child. A child may use the
database identifier returned by Odoo in a local JSON-2 load or the deterministic
External ID assigned to the parent in an import-capable remote load. Impodo
must journal that receipt before it releases the child.

The implementation must remain model-neutral. It must not add a
`product.category` or `parent_id` branch to preparation, preflight, or
execution.

## What failed

The affected workspace produced 1,936 generated Product Category rows and
selected 2,471 Product rows for preparation. Twelve categories were legitimate
roots, so their generated **Parent path key** was blank.

The current preparation path applies one rule to every resolved target identity
and target-scope component: the source value is required. It therefore changed
each root's deliberate blank parent into `SOURCE_REQUIRED_VALUE_MISSING`.
Quality evaluation then behaved safely but destructively:

1. It set aside the 12 blocked roots.
2. It set aside 1,924 descendant categories because their incoming parent was
   not ready.
3. It set aside 2,471 Products because their incoming category was not ready.

This produced 4,407 quarantined rows and no eligible row. The remaining 13,671
Product rows were already outside the explicit row-inclusion rule. The Final
review summary combined both groups under the set-aside total, which made the
failure look like thousands of independent data problems.

This result contradicts the implemented hierarchy design. That design says a
root's **Parent path key** stays blank and that an acyclic hierarchy reaches the
generic parent-before-child scheduler.

## Root cause

The defect is primarily a missing semantic distinction in the mapping and
compiled-plan contracts. It is not primarily an execution-precedence defect.

Impodo currently has only these two meanings for an identity component:

- A populated component contributes to identity or scope.
- A blank component is invalid because identity values are always required.

A hierarchy needs a third meaning:

- A blank value in an explicitly nullable target-scope component means that
  this record is a root. The null still participates in the complete business
  identity.

The generated hierarchy correctly supplies a blank parent key for a root. The
Match data presenter correctly suggests the self-referential parent field as
the target scope. The browser compiler then loses the root meaning because
`IdentityComponentMapping` and `IdentityComponent` carry no null policy.
`_prepare_identity_component` consequently parses every resolved component
with `required=True`.

The execution layer already contains the necessary ordering mechanism.
`plan_execution_rows` turns an incoming parent relationship into a hard row
edge when the relationship forms part of target identity or scope.
`schedule_dependencies` places the referenced row in an earlier component,
and `ExecutionService` requires a durable create receipt before it sends the
dependent row. Existing focused tests prove that general mechanism for an
acyclic hierarchy.

## Data-manager meaning

Suppose the prepared categories are:

```text
Finished
`-- Caps
    `-- Screw caps
```

The target identities should be:

| Category | Target key | Target scope | Meaning |
| --- | --- | --- | --- |
| Finished | `Finished` | explicit no parent | A root category named Finished. |
| Caps | `Caps` | reference to Finished | A category named Caps beneath Finished. |
| Screw caps | `Screw caps` | reference to Caps | A category named Screw caps beneath Caps. |

The blank scope on Finished does not mean that Impodo failed to find a value.
It means that the reviewed hierarchy deliberately puts Finished at the top
level. A blank category name would still be invalid.

For the data manager, this means that a valid root should not create a warning
or require an acknowledgement. A genuinely missing parent in the source should
still follow the selected **Stop and ask me to correct it** or **Set the
affected rows aside** policy.

## Recommended contract change

Add a closed null policy to target identity components in both browser mapping
and compiled Recipe contracts. A suitable portable shape is:

```json
{
  "source_column_keys": ["derived:category:parent_key"],
  "target_fields": ["parent_id"],
  "null_policy": "explicit_scope_null",
  "resolver": {
    "origin": "dataset",
    "dataset_id": "derived:categories"
  }
}
```

The supported values should initially be:

- `reject` keeps the current behavior and remains the default.
- `explicit_scope_null` treats a completely blank source key as one deliberate
  null target-scope value.

The validator should accept `explicit_scope_null` only when all of these
conditions hold:

1. The component belongs to `target_scope`, not `target_identity`.
2. The component targets one captured optional many-to-one field.
3. The resolver refers to the same generated hierarchical dataset.
4. The source column is that hierarchy's generated **Parent path key**.
5. The generated hierarchy contract identifies blank parent keys as roots.

These restrictions prevent a generic **Allow blanks** control from weakening
business keys elsewhere. For example, an Order Line must not silently lose its
required Order scope merely because Product Category roots permit no parent.

The mapping contract should advance from version 16. The compiled profile and
Recipe contracts should also advance because preparation semantics change. An
older contract must continue to mean `reject`; the application must not silently
reinterpret historical submitted evidence.

## Preparation behavior

The shared identity-component preparer should apply these rules:

1. A populated scalar component follows the existing typed parsing path.
2. A populated relational component produces the existing
   `LogicalReference`.
3. A completely blank component with `explicit_scope_null` produces one plain
   `None` scope value. It produces no `LogicalReference` and no source issue.
4. A partially blank composite key remains invalid.
5. Any blank component with `reject` retains the current required-value issue.

Using a plain `None` is preferable to inventing a portable sentinel that could
leak into an Odoo request. The compiled component policy and the mapping hash
preserve why that null is valid. Canonical staging validation must recheck the
row against that policy instead of assuming that every unaccompanied null is
valid.

The bounded evaluator and native columnar evaluator must produce identical
rows. Generated datasets currently use the bounded path, but fixing only that
path would leave Recipe or future direct-data behavior inconsistent.

## Quality and identity behavior

Quality evaluation must distinguish a valid null scope from an incomplete key.
It should:

- continue to reject a null target key;
- include an explicitly valid null target scope in the canonical collision
  key;
- emit no relationship-readiness edge for a root's null parent;
- retain the existing hard incoming edge for every populated parent; and
- set aside only the affected dependency branch when a real parent is missing,
  ambiguous, or blocked.

`quality_identity_key` currently excludes any identity containing `None`.
That rule should become policy-aware, or it should accept null scope values
after canonical staging has proved their component policy. Otherwise two root
records with the same normalized name could evade the intended collision
check.

The browser should group propagated failures beneath their first actionable
cause. It should show, for example, **12 root causes affect 4,395 dependent
records**, while keeping complete row accounting in support evidence. It should
not present thousands of dependency consequences as independent corrections.

## Odoo matching and precedence

Preflight should match every category by its complete reviewed identity:

```text
(category name, parent category identity)
```

For a root, the second component is explicitly null. For a child, it is the
parent's complete business reference. Impodo must never fall back to the leaf
name alone because the same name may exist beneath several parents.

Target precedence should work per hierarchy node:

1. If the complete key matches exactly one Odoo category, Impodo reuses or
   updates that record according to the reviewed mode.
2. If the complete key matches no Odoo category, Impodo plans a create.
3. If the complete key matches several records, Impodo blocks that branch.
4. A child may depend on either an existing parent or a parent created in the
   same load.

The target adapter should canonicalize Odoo's empty many-to-one representation
and the prepared explicit null to the same scope value. The live Odoo 19 tests
must also prove the exact domain representation used to find a root. This is
important because the portable meaning is null while a particular Odoo method
may represent an empty many-to-one as `False`, JSON `null`, or an empty import
cell.

## Load order and receipts

The safe load is a topological sequence, not a blanket two-pass category
update:

```text
Resolve existing complete keys
  -> create new roots
  -> journal root receipts
  -> create children whose parents are ready
  -> journal child receipts
  -> repeat for deeper levels
  -> create or update each Product after its selected category is ready
  -> read back and reconcile the exact relationships
```

Rows at the same depth may be sent in bounded compatible batches. Products may
start as soon as their own category dependency is satisfied; correctness does
not require waiting for unrelated deeper branches.

For local JSON-2 creates, Odoo returns numeric database identifiers. Impodo may
use those identifiers only inside the target-bound execution journal and
write process. They must not enter the Recipe, canonical prepared evidence, or
another target's plan.

For import-capable remote creates, Impodo should continue assigning
deterministic External IDs. A child can then reference the parent's External
ID after the parent's component has committed. Odoo's own import guidance
recommends External IDs when imported records must recreate relationships.

An acyclic hierarchy should require no relationship-completion pass. Impodo
should not create every category without `parent_id` and patch all categories
later because:

- the parent is part of the category's reviewed identity;
- the intermediate target state would contain records with the wrong scope;
- a timeout would leave more partially applied rows to reconcile; and
- the existing scheduler can satisfy the dependency before create.

The existing second write pass remains appropriate only for an explicitly
supported optional cycle. A parent relationship used as identity scope is a
hard dependency and cannot be deferred to break a cycle.

## Alternatives assessed

### Make every target scope optional

Reject this option. It would allow incomplete Order Line, company, warehouse,
or site scope and could match or create the wrong business record.

### Special-case Product Categories

Reject this option. The hierarchy feature also supports departments,
locations, analytic structures, and custom self-referential models. The
captured schema and generated hierarchy link provide enough generic evidence.

### Remove the parent from category identity

Reject this option. Repeated leaf names beneath different parents would become
ambiguous or merge incorrectly.

### Use the generated complete path as the only Odoo key

Do not use this as the general solution. The complete path is an excellent
incoming identity and External-ID input, but Odoo may not expose a stable,
writable, non-translated complete-path field for exact existing-record
matching. The governed name-and-parent business key remains necessary.

### Put every root beneath a fixed Odoo category

Reject this as an automatic repair. It changes the reviewed business hierarchy.
A data manager may choose a fixed parent during Source data authoring, but
Impodo must not invent one to avoid null handling.

### Create all nodes first and patch every parent later

Reject this as the normal path. It is safe only for optional relations whose
identity does not depend on the relation. Hierarchy parent scope is instead a
hard, acyclic dependency that the scheduler can satisfy in one ordered pass.

## Implementation ownership

| Responsibility | Owner |
| --- | --- |
| Portable nullable-scope policy | `domain/mapping/contracts.py` and `domain/recipe/profile.py` |
| Browser-to-runtime compilation | `domain/compiler/browser_mapping_compiler.py` |
| Hierarchy-specific authoring validation | `domain/mapping/validation` and `web/presenters/mapping_forms.py` |
| Shared prepared identity semantics | `domain/preparation/source.py` |
| Bounded and columnar parity | `domain/staging/evaluator.py`, `domain/compiler/columnar_transformation.py`, and `adapters/polars_transformation.py` |
| Null-scope collision and dependency quality | `domain/preparation/quality.py` |
| Existing-Odoo root canonicalization | `domain/preparation/preflight.py` and `domain/data_version/catalog.py` |
| Frozen row schedule | `domain/execution_snapshot.py` and `domain/execution/dependency_scheduler.py` |
| Receipt barriers and transport normalization | `application/workspace/execution/service.py` and the Odoo writer adapter |
| Root-cause summary | Final review presenters and prepared review artifacts |

## Delivery slices

### Implementation record

The contract, browser authoring, compilation, bounded preparation, collision
identity, Recipe reuse, and frozen execution-order tests are implemented.
Relational identity components still route through the Python oracle. The
columnar compiler recognizes the nullable policy and routes the whole dataset
to that shared implementation instead of executing the unsupported operation
natively. Historical version-16 mappings retain the original `reject` meaning.

The remaining operational qualification is a live local and remote Odoo 19
acceptance run plus the separate Final review improvement that groups propagated
dependency consequences beneath their actionable root cause.

### Slice 1: reproduce and freeze the semantics

- Add a failing end-to-end fixture with roots, children, repeated leaf names,
  and Products.
- Assert that the current failure starts with the roots and propagates to all
  dependants.
- Advance the mapping and compiled contracts with the closed nullable-scope
  policy.

**Exit result:** the contract can represent a deliberate root without allowing
arbitrary blank business keys.

### Slice 2: preparation and quality correction

- Compile the generated self-parent scope with `explicit_scope_null`.
- Prepare a root as `(name, None)` without an issue or logical parent edge.
- Preserve populated child references as hard edges.
- Include null-root scope in collision and accounting checks.
- Prove bounded and columnar semantic parity.

**Exit result:** roots, descendants, and their Products remain eligible unless
another real issue affects them.

### Slice 3: preflight and execution proof

- Prove exact matching for new and existing roots and children.
- Prove that parent-before-child components are frozen in the execution
  snapshot.
- Prove local numeric-receipt and remote External-ID relationship paths.
- Prove stop-on-rejection, unknown-outcome recovery, and exact read-back.

**Exit result:** no child write can precede its parent receipt, and an acyclic
hierarchy produces no deferred parent update.

### Slice 4: migration and user recovery

- Preserve historical version-16 mapping and preparation evidence unchanged.
- Create a new mapping revision for an affected hierarchy and require
  **Check matches** and **Check changes** again.
- Explain that the hierarchy-root behavior changed and that previous warning
  acknowledgements do not authorize the new plan.
- Show root causes separately from propagated affected-row counts.

**Exit result:** an existing project can recover without editing its frozen
source or silently changing an earlier approval.

## Acceptance criteria

1. A generated root with a blank parent is a candidate, not blocked or
   quarantined.
2. A blank target key remains blocked.
3. A child with a populated parent key resolves to exactly one incoming row.
4. Two equal leaf names beneath different parents remain distinct.
5. Two equal root identities trigger the normal collision policy.
6. A missing, ambiguous, excluded, or quarantined parent sets aside only its
   dependency branch and reports one grouped root cause.
7. A newly created root precedes every new child that references it.
8. A child receives the exact journalled parent identifier or External ID.
9. An existing root may satisfy a new child's dependency without a create.
10. Products load only after their own selected categories are ready.
11. Acyclic hierarchies have zero relationship-completion writes.
12. A hard parent cycle blocks before the first Odoo write.
13. Target lookup, create, and read-back remain bounded and do not add one
    target read per category.
14. Repeating the same confirmed load after an interruption cannot silently
    create duplicate roots or children.
15. Local and remote Odoo 19 acceptance runs reconcile the same hierarchy.

## Recovery for the observed project

After implementation, the affected workspace should not be repaired by
editing DuckDB rows or acknowledging the propagated warnings. Impodo should:

1. Preserve the failed preparation run as historical evidence.
2. Produce a new mapping revision that declares the generated parent scope as
   an explicit root-capable scope.
3. Ask the data manager to check and confirm that revised match.
4. Rerun preparation from the frozen source snapshot.
5. Show 12 valid root categories, their descendants, and the selected Products
   as eligible unless another independent issue remains.
6. Run **Check changes** again so Odoo matching and the parent-before-child
   execution snapshot are current.

The change should not alter the confirmed Tracking rules or the explicit
Product row-inclusion rule.

## Related documentation

- [Build hierarchical related records from separate source columns](multi-column-hierarchical-related-records.md)
- [Scalable relationship dependency planning and execution](scalable-relationship-dependency-planning.md)
- [Canonical staging contract](../developer/contracts/canonical-staging.md)
- [Quality and quarantine contract](../developer/contracts/quality-and-quarantine.md)
- [Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md)
- [Odoo 19 export and import guidance](https://www.odoo.com/documentation/19.0/applications/essentials/export_import_data.html)
- [Odoo 19 JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html)
