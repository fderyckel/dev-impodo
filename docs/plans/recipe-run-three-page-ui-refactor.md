# Recipe runs in three pages

## Status and decision

**Status:** Approved design direction and implementation plan, 2026-08-25.

This plan combines two ideas:

- make a Recipe run a short, guided journey; and
- keep the existing six-stage workspace only for creating or changing a
  Recipe.

A data manager who applies an existing Recipe sees no more than three pages:

1. **Fresh data**
2. **Check Odoo**
3. **Review and load**

Three pages is the maximum, not a requirement to stop three times. Impodo
automatically completes a page when it has current evidence and needs no
decision. A straightforward repeat on the same checked Odoo target can
therefore feel like a two-page run.

This changes the Recipe user interface, not the purpose of a Recipe and not
the safety of a run. Projects, data versions, Recipes, runs, workspaces,
preparation evidence, comparisons, confirmations, and verification all remain.

## The problem this solves

The current interface sends a data manager through the six authoring stages
after the Recipe already knows the required tables, relationships, Odoo
models, matching rules, and preparation rules. It also exposes setup and
Recipe work areas as separate journeys. The result feels like starting the
original design work again.

That is the wrong mental model. Creating a Recipe and using a Recipe are two
different jobs:

- **Create or change a Recipe:** inspect and decide; use the six stages.
- **Use an approved Recipe:** supply fresh inputs and review fresh results;
  use the short run journey.

## What a Recipe already knows

The Recipe supplies the reusable meaning for every run:

- which logical source tables are required;
- how those tables relate to each other;
- which Odoo models and fields are required;
- which Odoo supporting values must be refreshed;
- how source fields match Odoo fields;
- how values are cleaned, converted, and matched;
- which business checks must pass;
- which data must be prepared before other data; and
- which run-specific values or confirmations may be needed.

The data manager must not select this information again. A new run supplies
only what can genuinely change:

- the fresh source files or tables;
- the Odoo target for this run;
- any values that belong to this run, such as a stock date or warehouse;
- decisions about exceptions found in the fresh data; and
- final confirmation before a load.

## This is not a customer-only design

The same three pages apply to every supported business object. The labels and
questions come from the Recipe rather than from a customer-specific screen.

| Example Recipe | What **Fresh data** expects | What **Check Odoo** refreshes | What **Review and load** emphasizes |
| --- | --- | --- | --- |
| Customers | Customer rows and any source-owned supporting tables | Customer fields and reference values such as countries or payment terms | Duplicates, relationship matches, and customer results |
| Products | Product rows, categories, units, and other source-owned inputs required by the Recipe | Product fields and current Odoo choices such as categories and units | Required values, product matches, and load results |
| Products and bills of materials | Product and component tables named by the Recipe | Product, unit, and bill-of-material requirements | Parent-child completeness and the Recipe's load order |
| Stock balances | Stock rows plus the run's warehouse, location, or stock-date value when required | Current products, locations, units, and the supported Odoo stock boundary | Unknown products or locations, quantities, and stock-specific controls |
| Transactional data | The header, line, and supporting tables named by the Recipe | Current partners, products, taxes, journals, and other required Odoo references | Header-line completeness, ordering, totals, and the supported transaction load route |

Some object types need different checks or load methods. That difference
belongs inside the Recipe and the supported Odoo boundary. It must not create a
different six-stage user journey.

## The recommended journey

### Page 1: Fresh data

**Question answered:** Do we have the fresh inputs required by this Recipe?

The page shows the selected Recipe or Recipes at the top. It then presents one
upload area and a short checklist derived from those Recipes.

Impodo must:

1. accept all fresh files for the run in one place;
2. inspect the files and match their tables to the logical inputs expected by
   the Recipe;
3. show successful matches without asking for confirmation;
4. ask the data manager only when an input is missing or two tables could fill
   the same role;
5. collect run-specific values once, beside the data that needs them; and
6. save the accepted inputs as the run's fresh data version.

The page can show, for example:

> Product table found: `products.xlsx / Products`  
> Unit table found: `products.xlsx / Units`  
> Stock date: 31 August 2026  
> One decision needed: choose which sheet contains warehouse locations.

The Recipe's relationships appear as read-only meaning, such as “Stock rows
refer to Products by SKU.” There is no **Check related table** task and no
editable relationship screen.

The match uses a Recipe-owned logical input, not the old file name alone. A
new file may be called `August stock.xlsx` even if the authoring file was
called `stock_template.xlsx`, provided its content can safely fill the same
logical role. Ambiguous matches remain a data-manager decision.

The main action is **Use this fresh data**. It becomes available when every
required logical input and run-specific value is present.

### Page 2: Check Odoo

**Question answered:** Is the chosen Odoo target suitable for this run?

The data manager chooses or confirms the Odoo target. The Recipe then provides
the complete list of Odoo models, fields, reference values, and access checks
that Impodo needs. These requirements are visible as read-only information;
there is no general model picker.

The main action is **Check this Odoo**. One action performs one combined check
for all Recipes in the run. Impodo must:

1. confirm the target identity and access;
2. refresh the required Odoo model and field information;
3. refresh required supporting values in bounded groups;
4. compare the result with what each Recipe requires; and
5. create the internal Recipe work areas automatically after a successful
   check.

The data manager sees the business result:

- **Ready:** all Recipe requirements are available;
- **Ready with differences:** the Recipe remains usable and Impodo explains
  the differences that affect this run; or
- **Action needed:** access or an Odoo requirement prevents the run.

A new Odoo instance always receives fresh evidence. When the same target
already has current evidence for the same requirements, Impodo may complete
this page automatically and take the data manager to the final page. The page
must never reuse evidence from a different target.

There is no separate **Choose the Odoo data you need** task and no separate
**Create Recipe work areas** decision. Both are consequences of the selected
Recipe and target.

### Page 3: Review and load

**Question answered:** What happened with this fresh run, and is it safe to
load?

This page is the run's home from preparation through verification. Impodo
applies the saved Recipe automatically, while keeping the existing evidence
and safety gates.

The page moves through these states without sending the data manager into the
six-stage workspace:

1. **Preparing fresh data** — show progress and the current task.
2. **Action needed** — show only issues caused by this run's data or Odoo
   target, grouped by the Recipe that owns them.
3. **Ready for review** — summarize prepared rows, exclusions, warnings,
   relationships, and the proposed load.
4. **Check changes** — compare with the Odoo target using current evidence.
5. **Confirm and load** — require the existing explicit confirmation.
6. **Verify result** — show what Odoo accepted, rejected, or still needs
   attention.

With one Recipe, the page presents one review. With several Recipes, it shows
ordered cards such as **Products**, **Bills of materials**, then **Stock
balances**. The Recipe dependencies decide the order. The data manager does
not arrange it again.

A card opens details only when the data manager asks or when an issue needs a
decision. A clean card stays compact. If comparison finds no changes, the page
finishes without asking for a load confirmation.

## One run, one visible place

The run becomes the visible home of the work. Impodo may still create setup
and Recipe-application workspaces because they contain required evidence, but
those workspaces are implementation containers rather than separate user
journeys.

The top of every page shows the same small run header:

> Project / Test run / Recipe set  
> Fresh data — Check Odoo — Review and load

Only one page is active. Completed pages show their result. A page with an
issue shows an **Action needed** marker. There is one obvious main action on
each page.

## Navigation and recovery rules

The first delivery must stop the circular journey even before all three pages
are fully combined.

- Authoring keeps **Source data**, **Odoo data**, **Match data**, **Prepare**,
  **Final review**, and **Load**.
- Test and Production Recipe runs use **Fresh data**, **Check Odoo**, and
  **Review and load**.
- A Recipe-application workspace must not show editable Source, Odoo, mapping,
  or relationship-authoring stages.
- A return action always returns to the owning run and the page that owns the
  current issue.
- Existing saved URLs may continue to resolve, but Recipe-run URLs redirect to
  the correct run page instead of exposing a second workflow.
- Browser Back must not create another run, upload, Odoo check, preparation,
  or load attempt.

Each issue has one owner:

| Issue | Owning page |
| --- | --- |
| Missing or ambiguous source input | **Fresh data** |
| Missing run-specific value | **Fresh data** |
| Wrong target, missing access, or incompatible Odoo requirement | **Check Odoo** |
| Fresh-data quality, duplicates, or unmatched relationships | **Review and load** |
| Load rejection, uncertain outcome, or verification difference | **Review and load** |

No issue may send the data manager to a generic stage without explaining what
must be decided there.

## What remains unchanged

This refactor preserves the current ownership and evidence:

- The Project remains the business and governance home.
- Each accepted source package remains a data version.
- A Recipe remains reusable, versioned meaning rather than copied source or
  target data.
- Test and Production remain separate runs with separate evidence.
- Workspaces continue to contain detailed preparation and comparison evidence.
- Odoo credentials and the target identity remain outside the portable Recipe.
- A new target receives fresh Odoo evidence and fresh supporting values.
- Mapping, preparation, and review evidence remain bound to their exact inputs.
- **Check changes**, **Confirm and load**, and **Verify result** remain guarded
  actions.
- Duplicate, stale-evidence, access, and uncertain-outcome safeguards remain.
- Production never inherits a Test load confirmation or credential.

## Delivery plan

### Phase 1: stop the circular journey

Introduce Recipe-run navigation and return rules. Hide the six-stage
navigation in setup and Recipe-application workspaces. Route every existing
blocker to its owning run page. Keep the underlying pages temporarily where
necessary, but make the run feel like one journey.

**Exit result:** a data manager cannot accidentally re-enter Recipe authoring
while using a Recipe.

### Phase 2: build Fresh data

Combine run creation, file acceptance, logical-input matching, and run-specific
values. Store an explicit match between each Recipe input and each accepted
fresh table. Use content evidence as well as names. Reuse a common run cutoff
for standard date needs unless a Recipe explicitly requires another value.

**Exit result:** fresh data is supplied once, and related source inputs are
requested from Recipe knowledge rather than rediscovered by the user.

### Phase 3: build Check Odoo

Replace the model picker with Recipe-derived, read-only requirements. Check all
selected Recipes in one bounded target operation. Automatically create the
internal setup and application workspaces after success.

**Exit result:** the data manager chooses the target once; Impodo decides what
must be refreshed and checked.

### Phase 4: build Review and load

Make the run page the home for preparation progress, current-data issues,
review, comparison, confirmation, load progress, and verification. Present
several Recipes in dependency order. Use saved job summaries for progress
rather than reopening every workspace.

**Exit result:** the data manager stays on one page from automatic preparation
to verified outcome.

### Phase 5: apply the journey to Production

Use the same three-page language and layout for an approved Production run,
while creating fresh Production evidence, resolving Production credentials,
and preserving every Production safety gate.

**Exit result:** Test and Production feel consistent without sharing authority
or evidence that must remain separate.

### Phase 6: finish the user guidance

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
