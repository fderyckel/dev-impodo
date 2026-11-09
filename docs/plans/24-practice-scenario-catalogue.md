---
audience: developer
kind: plan
status: proposed
---

# Proposed practice scenarios and qualification evidence

## Purpose and status

This catalogue proposes exercises for a data manager and acceptance cases for
a coding agent using the same fictional business data. The proposed corpus
has separate canonical customer and manufacturing owners. The
[current customer pack](../../scenarios/impodo-practice/v1/README.md) owns the
customer subset, and the [manufacturing pack](../../scenarios/impodo-practice/manufacturing/v1/README.md)
owns supplied categories, combined products, separate headers, lines and
operations. Each owner records its actual qualification scope. The remaining
proposal has two CSVs and fourteen rows under `docs/plans/24-shared-practice-data/`.
The cases below remain proposal acceptance requirements unless their canonical
owner explicitly records their execution. In particular, the supplied-header
manufacturing exercise does not qualify the separate related-split provider.
Proposed fault JSON files do not establish completed runtime fault checks.

Read the [shared data proposal](24-shared-practice-data-and-scenarios.md) for
the business story, delivery plan, and ownership of the remaining fixtures.

The reader should use this catalogue to choose an exercise, identify its
expected result, and decide whether a failure belongs to the input data,
Impodo, or the test environment. The cases below are proposed acceptance
requirements. References to existing tests show a current behavior or a known
regression; they do not establish that the new corpus has passed that test.

Use a small subset for each first exercise. A learner should be able to inspect
the affected records by eye before running the whole business story. Keep the
larger delivery for combined workflows and generated scale trials.

## One exercise, two ways to run it

A human exercise explains the business goal, the files to select, the choices
to make, and the result to inspect. It should use the current browser labels
when the exercise is implemented. An agent case uses those same records and
business decisions through the actual application boundaries and asserts the
saved evidence and target result. An agent must not bypass a stage by manually
constructing the representation that the stage is meant to produce.

Each implemented exercise needs an independently reviewed answer that names:

- The source delivery and the business keys of its affected records.
- The target seed, supported Odoo major, installed modules, operation company,
  and required read and write permissions.
- The mapping, row selection, relationship, and normalization decisions.
- The expected prepared values and the exact fields that may change.
- The expected comparison classifications and any expected blockers.
- The final target values, verification result, and repeat-comparison result.
- The recovery or cleanup action and the evidence that limits its scope.

Define expected values before running Impodo. Do not generate the answer from
the manifest or preparation output being checked. Numeric Odoo IDs may appear
in private operational receipts, but expected business results must identify
records by portable keys and company or hierarchy scope.

## Priority 0: the shared practice journey

### UC01. Combine partial customer and supplier records

The current customer runner implements the three-file exact combination,
compound join keys, company row selection, cleaned contact fields and address
mapping. Customer payment terms belong to UC04. Supplier roles, VAT, Country,
Language and additional relationship kinds remain extensions for this pack.

The data manager receives several exports that describe the same contacts.
They select an exact combination using the contact key and choose the final
fields once. They keep customer and supplier payment-term roles distinct,
including contacts that have both roles.

The agent must create the combination through the application, save it,
reopen it, and prepare its output. It must assert the output grain, retained
fields, row count, and source lineage. A second run changes one source value
and must produce the corresponding new prepared value.

An intentionally duplicated added-table key or a missing required match must
stop the combination with a useful explanation. Silently multiplying rows,
choosing one duplicate, or losing an optional blank is a product failure.

Existing evidence:
[exact combination workflow](../../tests/integration/web/test_exact_join_workflow.py)
and [grouped preparation](../../tests/integration/artifacts/test_grouped_exact_join_preparation.py).

### UC02. Generate and supply the same category hierarchy

The data manager first builds categories from three ordered level columns.
They then repeat the task with an explicit category table containing parent
keys. The data contains repeated names under different parents, a root with
no parent, and products assigned at intermediate and leaf levels.

Both forms must produce the independently declared complete paths and product
assignments. A repeated leaf name must not merge unrelated branches. The
agent must save and reopen the mapping, load parents before dependent
children, verify the exact paths, and repeat the comparison without new
creates.

A populated unknown parent, a partially blank compound key, or a parent cycle
must remain a blocker. None may silently become a root. A root null is valid
only under the reviewed root policy.

Existing evidence:
[independent hierarchy cases](../../tests/support/hierarchy_cases.py),
[Authoring workflow](../../tests/integration/web/test_hierarchy_workflow.py),
[Recipe replay](../../tests/integration/web/test_hierarchy_recipe.py), and
[supplied hierarchy](../../tests/integration/web/test_same_table_hierarchy.py).

### UC03. Prepare products, BOM headers, lines, and operations

The data manager combines partial views of BOM headers without flattening
their lines or operations. They map lines and operations as separate child
tables and connect each to its incoming header. They review unit conversion
and quantity rules before preparing.

The agent must assert the distinct grains, header keys, line identities,
operation identities, complete lineage, and final quantities. It must verify
that a product variant generated by Odoo is read back before a dependent BOM
line uses it. It must check exact relationships and values after loading,
rather than accepting matching aggregate totals alone.

Do not infer a universal meaning for a zero quantity denominator. A case with
that value needs an explicitly reviewed business rule or an expected blocker.
The existing PLW demonstration fallback is specific to its own rule.

Existing evidence:
[browser source composition](../../tests/integration/web/test_bom_source_composition.py),
[Recipe composition](../../tests/domain/recipe/test_bom_source_composition.py),
[execution dependencies](../../tests/application/workspace/execution/test_service.py),
and [scenario qualification](../developer/runbooks/scenario-qualification.md).

### UC04. Create a payment term used by existing and new contacts

This is the mandatory regression case for the lost incoming dependency. Use
one existing contact, one new contact, and one incoming payment term that does
not yet exist in the target. Both contacts must use that new term. Record every other intentional mapped change explicitly. The draft baseline
also changes a contact name; observing that name update alone does not prove
that the incoming term dependency survived.

The term's meaning includes its reviewed payment schedule. Stock Odoo 19 and
20 generate one 100% zero-day line with a new term. The current baseline
therefore creates or reuses headers, captures and updates the generated NET45
line to 45 days, then assigns customers in a third normal run. It creates no
additional schedule line. The same-load dependency regression uses a separate
reviewed immediate-term variant whose intended schedule is the stock zero-day
default. Both workflows verify the complete schedule independently.

The human maps the relationship to the incoming payment-term table, prepares
the data, compares with Odoo, reopens the saved workspace, and reviews the load
before confirming it. The agent must exercise the complete chain:

1. Accept the source delivery and save the real mapping.
2. Run actual preparation and comparison against the target seed.
3. In the immediate variant, assert one new header, one contact create, one
   contact update and one reused NET30 header. Do not map a second 100% line.
4. Save the resulting execution snapshot, reconstruct the application
   services, and read that saved snapshot again.
5. Assert that both contact relationships still identify the incoming term.
6. Load through the normal execution service. Assert that the header's durable
   receipt precedes both dependent contact writes. Independently verify the
   one generated zero-day schedule. In the staged NET45 workflow, verify the
   generated-line update before customer assignment.
7. Read the target independently and assert that both contacts point to the
   newly created term in the selected company. Assert the exact scoped term
   projection and its complete schedule, including the inverse header links.
8. Reconcile and compare again. All mapped rows must be unchanged. Verify the
   immediate schedule independently because it is generated by Odoo rather
   than included as another mapped line.

Run the same responsibility with a term that already exists, a newly created
owner, an existing owner, scoped keys, and supported list relationships. Do
not manufacture an old snapshot shape to stand in for real comparison.

Looking up the incoming term as an already existing target record before
creating it is an Impodo defect. Changing the fixture to pre-create the term
would hide the regression.

Existing evidence:
the concurrent, unmerged incoming-update regression at
`tests/application/workspace/execution/test_incoming_updates.py`
and [execution snapshot](../../src/impodo/domain/execution_snapshot.py).

### UC05. Repair customer and vendor payment terms

The fictional target starts with a supplier whose vendor term was placed in
the customer-term field. The data manager runs an update-only repair. They
fill the vendor field and explicitly clear the incorrect customer field.
Another contact retains a legitimate customer term and must remain outside
that clearing decision.

The comparison must propose zero creates and changes only to the reviewed
payment-term fields. Read-back must show the intended vendor term, the
explicit customer blank, and preservation of the legitimate customer value.
Leaving a field unmapped must not be treated as clearing it. An ignore-blank
policy must preserve the existing value when that is the selected policy.

This exercise uses a fresh update workflow. It must not claim that
completed-load correction can move a mapping between target fields.

Existing evidence:
[blank relationship preparation](../../tests/domain/preparation/test_blank_create_relationships.py),
[Recipe supporting decisions](../../tests/domain/recipe/test_supporting_decisions.py),
and [current correction boundary](../user/workflow/06-load-into-odoo.md#correct-a-verified-authoring-load).

### UC06. Keep BOM lines only when their parent is included

The source contains an active header with children, an excluded inactive
header with children, and a child whose parent is absent. The data manager
reviews those conditions, then chooses to use only children whose parent is
included.

The agent must first prove that an unfiltered child mapping reports the
missing included parents. After the reviewed inclusion rule, it must assert
the retained children and separately account for excluded-parent and
absent-parent consequences. No excluded child may be written. A later Recipe
delivery must recompute membership from its own rows.

Existing evidence:
[included-parent rules](../../tests/domain/mapping/test_included_parent_rows.py),
[included-parent browser workflow](../../tests/integration/web/test_included_parent_rows.py),
and [Recipe hierarchy replay](../../tests/integration/web/test_hierarchy_recipe.py).

### UC07. Review ambiguous values and deliberate blanks

The data manager reviews a short set of source duplicates, ambiguous target
keys, an unknown reference, an optional blank relationship, a missing
create-required value, a localized decimal, and a changed selection choice.
Each condition belongs to a named record and has one stated business decision.

The agent must assert exact prepared values and reason codes. It must prove
that optional blank, explicit clear, unknown value, and incomplete compound
key are different states. A duplicate-exclusion exercise must keep the chosen
row and its dependent relationships, without changing the surviving identity.

Existing evidence:
[duplicate exclusions](../../tests/integration/web/test_duplicate_row_exclusions.py),
[required values](../../tests/integration/web/test_required_values.py),
[optional lookup blanks](../../tests/domain/preparation/test_lookup_blank_source.py),
and [canonical examples](../developer/reference/examples-and-edge-cases.md).

### UC08. Load, verify, and explain the totals

The data manager completes a clean load and reviews its result. A separate
controlled target changes one mapped field after accepting a write. That
second exercise must show successful transport followed by a verification
difference, with the exact affected field and prepared and observed values.

The agent must independently assert source rows, selected rows, prepared rows,
excluded rows, comparison classifications, writes, rejected rows, uncertain
rows, and verified rows. A source exclusion is not an upload rejection. A
successful HTTP response is not proof of a committed import. A committed
record with a field difference is not a record that was never created.

The clean case must reconcile and produce an unchanged repeat comparison.
The changed-field case must retain the discrepancy and must not falsely
report complete verification.

Existing evidence:
[execution service](../../tests/application/workspace/execution/test_service.py),
[scenario execution evidence](../../tests/integration/scenarios/test_execution_evidence.py),
and [execution contract](../developer/contracts/execution-and-reconciliation.md).

### UC09. Reuse the rules on a fresh delivery

The data manager publishes Recipes and applies them in an integrated Test run
using another delivery. The supplied May delivery changes selected business
values and row order; it expects no customer creates. Qualify an additional
new-record delivery as a separate extension. Use a second target with different
numeric record IDs for portability. A missing-required-column variant also
needs its own derived delivery; it is not supplied by the current May files.

The agent must use actual Recipe compilation and application, fresh source and
target evidence, and the new delivery's expected results. It must verify
relationship roles, intentional blanks, hierarchy order, row rules, and
combination lineage. It must prove that physical paths, source rows, target
IDs, authoring approvals, and old receipts did not enter reusable meaning.

A missing or ambiguous required logical input must block application. The
agent must not silently bind a similar heading. Application must not publish
a Recipe successor or rewrite the immutable source or Recipe revision.

Existing evidence:
[Recipe lifecycle](../developer/contracts/recipe-lifecycle.md),
[source-composition replay](../../tests/domain/recipe/test_source_composition_round_trip.py),
and [integrated Recipe runs](../../tests/application/run/test_integrated_recipe_runs.py).

## Priority 1: deliberate failures and recovery

Apply one named variation at a time to the clean business story. Declare the
expected failure before running it. Keep transport faults, identity changes,
permission changes, and server rewrites in controlled target behavior rather
than disguising them as source CSV values.

| Case | Human or agent action | Required result and evidence |
| --- | --- | --- |
| UC10: Existing or incoming reference. | Compare exact, case-only, missing, and ambiguous units or payment terms. Review each distinct business choice. | Existing and incoming origins remain visible. A case-only candidate requires an explicit decision. Dependent rows share that decision. Ambiguity never selects an arbitrary match. |
| UC11: Hierarchy defects. | Introduce a duplicate root, an absent parent, an excluded parent, and a cycle in separate variations. | Each keeps its distinct reason and affected group. Children do not become roots. Fixing one cause produces a new checked revision without rewriting failed evidence. |
| UC12: Supporting access changes. | Renew only the key, then separately change the company, principal, permissions, or fields. Reopen the page after each action. | A verified key-only renewal can retain valid choices. Material changes require review. A denied lookup reports that it did not fetch records; an empty list does not prove absence. |
| UC13: Conflicting saved choices. | Save a related-data choice that conflicts with a field rule. Check it, reopen, and correct the named field, including when it is on another page. | The real field-specific blocker stays visible. The draft survives. Repeating the same confirmation is not presented as the correction. |
| UC14: Known batch rejection. | A controlled target rejects a batch containing one faulty row and innocent peers. Exercise the selected continuation and isolation policy. | Rows affected by a rejected request are distinguished from proven bad inputs. Independent writes, dependency fallout, failure budgets, pause, stop, and guarded continuation follow the current contract. |
| UC15: Interrupted write. | Interrupt before dispatch, after target commit, and after durable receipt in separate runs. Reconstruct services and request recovery. | Exact read-back governs recovery. A committed parent is not created twice. Children remain blocked until the parent is proved. Unknown outcomes never trigger blind retries. |
| UC16: Stale evidence. | Change the comparison binding, target identity, current company, or referenced parent after review. | The load stops before journal or transport where required. A stale approval is not reinterpreted as approval of new meaning. |

Focused existing evidence includes
[target-first relationships](../../tests/domain/preparation/test_target_first_relationships.py),
[hierarchy failures](../../tests/integration/web/test_hierarchy_failures.py),
[supporting recovery](../../tests/integration/web/test_mapping_supporting_recovery.py),
[supporting access revalidation](../../tests/integration/web/test_supporting_access_revalidation.py),
[load controls](../../tests/integration/web/test_load_controls.py),
[failure budgets](../../tests/domain/execution/test_load_control.py),
[hierarchy recovery](../../tests/integration/web/test_hierarchy_recovery.py), and
[load recovery](../../tests/integration/web/test_load_recovery.py).

### UC19. Accept a native Excel worksheet containing empty rows

The learner uploads a native XLSX companion containing an empty interior row,
a populated row whose first cell is empty, and trailing empty rows. They save
the selected tables without editing the workbook to remove those rows.

The agent must run the real source inspection and snapshot-publication path.
It must assert that fully empty rows are skipped, later populated rows retain
their physical Excel row numbers, and a populated row with an empty first
cell is retained. The original workbook bytes and source hash must remain
unchanged. Snapshot creation must finish before any Odoo operation.

An `EmptyCell.row` exception or renumbered source evidence is an Impodo
regression. This is a container-specific exercise: a CSV representation does
not establish that the XLSX reader works. The proposed CSV pack does not yet
include this native XLSX companion, so the new corpus exercise remains
pending until that artifact and its independent row-position answer exist.

Existing implementation and controlled evidence:
[source file reader](../../src/impodo/application/data_version/source_files.py)
and [sparse Excel publication](../../tests/integration/web/test_sparse_xlsx_freeze.py).

### UC20. Disconnect a mapped relationship and reopen it

The learner connects a source value to a relationship, saves it, checks it,
then chooses **Do not fill this field**. They save and check again and reopen
the saved mapping. The relationship assignment must stay removed. This choice
stops writes to that field; it does not clear values already stored in Odoo.

The agent must submit the real form shape, including the stale source-control
values that hidden controls can still submit. It must assert that the
explicit disconnect choice wins, the relationship assignment is absent after
save and reopen, and unrelated fields remain intact. Cover Odoo 19 and 20,
local and remote configurations, and compatible older submissions.

Recreating the source connection from hidden stale controls is an Impodo
regression. The concurrent fix has controlled regression coverage in
relationship disconnect tests in the concurrent, unmerged
`tests/integration/web/test_relationship_disconnect.py`.
That evidence uses simulated readers; live browser behavior remains
unverified. It does not establish acceptance of this proposed corpus exercise.

## Priority 2: transfer and scale

UC17 captures the same fictional business records from Odoo and transfers them
to a different target of the same major. It must exercise capture scope,
relationship closure, frozen evidence, destination matching, selected rows,
dependency ordering, preflight, load, and verification. Test 19 to 19 and 20 to
20 independently. Both cross-major directions must stop before writing.
Odoo-source Production remains outside the supported lifecycle.

Existing evidence is the
[transfer journey](../../tests/integration/web/test_odoo_transfer_journey.py)
and [row transfer qualification](../../tests/integration/odoo/test_row_transfer_qualification.py).

UC18 generates larger deliveries from the same declared business rules.
Measure physical rows, distinct hierarchy nodes, depth, fan-out, number of
failing groups, connections, schema checks, elapsed time, and peak memory.
Increasing product rows while retaining a tiny category tree does not qualify
hierarchy growth. Keep generated large files out of the everyday learner
download and report controlled measurements separately from live timings.

## Required qualification matrix

For UC01 through UC09 and affected recovery cases, record each applicable
combination independently:

| Odoo major | Access mode | Authoring | Recipe application |
| --- | --- | --- | --- |
| 19 | Local | Controlled and live are separate results. | Controlled and live are separate results. |
| 19 | Remote | Controlled and live are separate results. | Controlled and live are separate results. |
| 20 | Local | Controlled and live are separate results. | Controlled and live are separate results. |
| 20 | Remote | Controlled and live are separate results. | Controlled and live are separate results. |

Local mode must use the actual local read boundary and the applicable normal
write boundary. A remote test with a loopback URL renamed in fixture metadata
does not establish local execution. Each live result must record the exact
reported final build, edition, modules, target seed, scenario revision, and
transport. Derive metadata and reference policies from the verified major.

The new corpus has no completed live matrix in this proposal. Existing
controlled tests and earlier live canaries remain evidence for their own
inputs and scope. A missing executable, unavailable target, invalid seed,
incomplete cleanup, or missing module is an omitted or failed environment
check, never a scenario pass. A controlled pass cannot fill a live cell, and
an Odoo 19 pass cannot fill an Odoo 20 cell.

Before promoting any exercise as qualified, require independent prepared
values, final target projection, read-back, reconciliation, and unchanged
repeat comparison where writes are expected. Count-only agreement is
insufficient. An expected blocker passes only if its reason, affected scope,
retained evidence, and absence of prohibited writes are asserted.

## Current runner limitations

The existing `impodo-cli scenario` command provides a profile-driven
qualification slice. It validates definitions and contained artifacts, runs
preparation and comparison, and supports governed file-to-Odoo round trips,
independent projections, reconciliation, and repeat comparison. Remote HTTPS
writes require an exact non-secret target identity binding.

The command does not create normal Project, Data version, workspace, or Recipe
application records. It does not provision or reset a target, independently
attest a seed, capture an Odoo source, drive a browser, or automatically clean
a remote target. A schema accepting Recipe references or an Odoo source is
not proof that the current runtime executes those journeys.

Use application integration tests for the full Authoring and Recipe chain.
Use an explicitly requested isolated browser trial for human acceptance. Keep
the CLI canaries as smaller checks of their declared responsibilities. Do not
describe a CLI round trip as completed browser or Recipe qualification.

Live trials must use allocated disposable targets and private evidence
directories. Cleanup must use owned receipts and dependency order; it must
not delete pre-existing records merely because a business key resembles a
fixture. These requirements follow the current
[qualification runbook](../developer/runbooks/scenario-qualification.md),
[hierarchy runner instructions](../../scripts/README.md),
and [version qualification boundaries](14-odoo-19-and-20-support.md).

## Failure classification

| Classification | Example | What the case must establish |
| --- | --- | --- |
| Expected data decision. | A line refers to an excluded parent; a case-only term needs review. | Impodo explains the affected records and next decision, retains the input, and prevents unsafe writes. |
| Impodo regression. | Comparison loses an incoming dependency on an existing-record update. | Correct source and mapping reach the real failing boundary. A repaired implementation passes without weakening the fixture or bypassing that boundary. |
| Environment failure. | Odoo is unreachable, the required module is absent, or the target seed cannot be verified. | The run records the failed or omitted prerequisite. It makes no claim about later stages. |
| Target rejection with unknown cause. | An import returns a generic known rejection without a classified rule. | The run retains safe context and write certainty. It does not invent a VAT or field diagnosis. |
| Uncertain write. | The target may have committed but its response was lost. | The journal retains uncertainty, later dependent writes stop, and exact read-back precedes any recovery decision. |

Early tax validation and automatic exclusion of newly discovered server-rule
failures remain proposed. Conventional continuation, isolation, failure
budgets, durable stop, and guarded continuation have controlled implementation
coverage. Keep those claims separate as recorded in
[the tax and continuation plan](20-tax-validation-and-rejected-record-continuation.md).
