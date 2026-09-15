# Complete the three-page Production run journey

## Status and remaining scope

**Status:** Run-owned navigation and the Test run's **Fresh data**, **Check
Odoo**, and **Review and load** journey are implemented. Production journey
completion and remaining user guidance are open as of 2026-09-15.

Current Test behavior belongs to the [integrated Test developer workflow](../developer/workflow/07-integrated-test-runs.md)
and [user guide](../user/guides/integrated-test-runs.md). Completed implementation
phases remain in Git history. Authoring retains the six-stage workspace.

Production already uses run-owned navigation; complete the accepted three-page
journey while retaining fresh Production evidence and its separate authority.

## Remaining delivery

### Complete Production behavior

Use the same three-page language and layout for an approved Production run,
while creating fresh Production evidence, resolving Production credentials,
and preserving every Production safety gate.

**Exit result:** Test and Production feel consistent without sharing authority
or evidence that must remain separate.

### Complete current guidance and visual qualification

Update the Concepts page, Test and Production guides, tutorial, workflow map,
screenshots, and accessibility evidence after each phase becomes current. Do
not describe planned pages as available before their implementation is
verified.

## Performance and Odoo 19 boundaries

The short UI must also be a short operation, not six hidden stages executed as
many repeated requests.

- Build each run page from one bounded run summary.
- Do not open every Recipe workspace database just to render the run page.
- Combine required Odoo models and fields before contacting Odoo.
- Read supporting Odoo values in bounded model-and-field batches.
- Do not perform one schema, permission, or relationship request per row.
- Render background progress from saved job summaries while a worker owns a
  workspace.
- Record and test request counts for multi-Recipe product, bill-of-material,
  stock, and transactional runs to prevent N+1 behavior.
- Keep the Odoo connection boundary aligned with Odoo 19 conventions and the
  currently supported load operations. A Recipe cannot make an unsupported
  Odoo business action safe merely by naming it.

## Production acceptance boundaries

The shorter journey must not hide decisions that change business meaning.
The following constraints apply to the remaining Production work:

- **Exact Recipe version:** each run uses the approved Recipe revision selected
  for that run. It never silently changes to the newest revision.
- **Meaning of the fresh data:** the run records whether the files represent a
  full replacement, additions and changes, or a dated balance. This is
  especially important for stock and transactions.
- **Explainable source matching:** Impodo shows why each table matched a Recipe
  input and asks when more than one match is credible. A familiar file name is
  not enough evidence by itself.
- **Odoo differences beyond field names:** target checks include the access,
  company choices, supporting values, archived records, and business settings
  that the Recipe actually relies on.
- **Current supporting values:** knowing that a related Odoo table is required
  does not mean its current records are known. Impodo refreshes only the
  Recipe-owned supporting values and explains why they are needed.
- **Object-specific load boundaries:** customer, product, bill-of-material,
  stock, and transactional Recipes share the journey but keep their supported
  Odoo operation, dependency order, and business checks.
- **Partial work and safe recovery:** a stopped preparation, uncertain load, or
  partial rejection resumes from its saved evidence. Retrying must not create
  duplicate Odoo work.
- **Evidence age and target identity:** automatic completion is allowed only
  for evidence that is current for the exact Odoo target and Recipe
  requirements.
- **Several Recipes together:** shared inputs and Odoo checks are combined,
  while conflicting rules and dependency cycles remain visible decisions.
- **A focused final page:** **Review and load** groups issues by their business
  owner and opens detail progressively. It must not become one long technical
  exception list.
- **Separate Production authority:** Production may reuse approved Recipe
  meaning, but it creates fresh target evidence and requires its own access,
  comparison, confirmation, and verification.

## Verification examples

The browser and service tests must cover at least:

1. a clean customer Recipe on fresh customer data;
2. a product Recipe with refreshed categories and units;
3. products followed by bills of materials with parent-child dependencies;
4. stock balances with products, locations, quantities, and a run-specific
   stock control;
5. a supported transactional header-and-line Recipe;
6. several Recipes sharing one Odoo check without repeated target requests;
7. a renamed file that safely matches the same logical Recipe input;
8. missing and ambiguous source inputs;
9. a new Odoo instance with compatible requirements;
10. a changed or inaccessible Odoo instance;
11. current evidence on the same target that safely skips a manual stop;
12. current-data quality and relationship issues routed to the final page;
13. zero proposed changes, partial rejection, and verification differences;
14. repeated submissions and browser Back without duplicate work;
15. saved direct links redirecting to the correct run page;
16. keyboard, focus, status-message, narrow-screen, and zoom behavior; and
17. a Production run that uses the same journey with separate evidence and
    authority.

## Acceptance criteria

The refactor is complete only when:

- a Recipe run has no more than the three named pages;
- fresh inputs are uploaded once and the Odoo target is chosen once;
- the Recipe supplies required tables, relationships, Odoo requirements,
  rules, and dependency order without asking the data manager to recreate
  them;
- Impodo asks only for missing, ambiguous, changed, or run-specific decisions;
- customers, products, bills of materials, stock, and supported transactions
  use the same page structure;
- the six stages remain available for Authoring and do not appear as the Recipe
  run journey;
- every issue remains visible and returns to one owning page;
- multi-Recipe pages and Odoo checks have bounded access with no hidden N+1
  behavior; and
- the existing evidence, confirmation, target, access, duplicate, stale, and
  verification protections remain effective.
