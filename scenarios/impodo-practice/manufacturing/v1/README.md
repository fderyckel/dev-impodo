# Morrow Assembly manufacturing practice

Use this pack to practise a manufacturing migration with category paths, product enrichment, separate BoM headers, component lines and operations. Users and coding agents work from the same fictional source files, starting states and independent business answers.

Each delivery contains six source files and 118 physical rows: 12 categories, 32 product core rows, 32 product detail rows, 6 BoM headers, 24 component rows and 12 operations. The five original baseline files retain their 112 rows byte-for-byte. The new header export supplies six separate fictional business records. A second delivery contains all six files with the same business values in a different order. It adds no changed price, date or product that lacks a declared answer.

The target is a disposable Morrow Assembly database with two operation companies: Morrow Assembly North and Morrow Assembly South. One load selects one operation company. Products and categories are shared; BoMs and work centres belong to the selected company. Completing both product partitions is required before either BoM exercise because components cross the source partitions.

## Files and target setup

Use `manifest.json` for source hashes and counts, `target-seed-projection.json` for staged initial target intent, `exercise-contracts.json` for composition and mapping decisions, and `expected-business-results.json` for independent final business values. These files own the exercise's source and business answers. The answers were declared before the manufacturing runner and are checked against the source.

The target must expose the normal models and fields supplied by `base`, `product`, `stock` and `mrp`. Capture and verify metadata on the actual Odoo 19 or Odoo 20 target. A customer-only database or an installed module list without those captured fields is insufficient. No enterprise feature, accounting localization or tax correctness is assumed.

Set up only the records owned by this disposable allocation. Resolve the existing Units unit; do not create another unit for source label PCE. Create the TRAINING and MORROW tags and four work centres through ordinary Odoo APIs. Assign MA-WC-01 and MA-WC-03 to North, and MA-WC-02 and MA-WC-04 to South. Odoo must create the work centres' resources normally. Retain an exact private ownership ledger for cleanup. Existing native references stay outside that ledger and are never adopted as fixture-created records.

Before seeding, verify that the exact native XML ID `product.product_category_services` already resolves to the shared Services root with no parent or children. On Odoo 19 it has no company field; on Odoo 20 its company is unset. This is an existing, unowned reference: never create, rename, delete or adopt it. Seed only the two owned categories Components and Components / Metals. The first category run therefore starts with three available paths. Run it before seeding the eight existing products: several seeded products need descendant categories. After all 12 category paths exist, seed products MA-P-0009 through MA-P-0016 with the values in the seed file. MA-P-0010 deliberately starts with name Practice Component 02 Before Review and price 1.00. Do not pre-correct it. The initial target has no Morrow BoMs, lines or operations.

The setup command implements these two seed steps without loading your migration. Use the [private descriptor format](../../v1/README.md#run-the-application-checks) with a fresh manufacturing target and the exact supported major. For example, prepare an Odoo 20 allocation:

```sh
python -m scripts.prepare_practice_manufacturing_target \
  --target-descriptor /private/morrow/manufacturing20.json --expected-major 20 \
  --stage initial --seed-directory /private/morrow/new-manufacturing-seed --allow-write
```

Load the categories in your existing Impodo session. After all twelve paths match the answers, continue using the same descriptor and private seed directory:

```sh
python -m scripts.prepare_practice_manufacturing_target \
  --target-descriptor /private/morrow/manufacturing20.json --expected-major 20 \
  --stage existing-products --seed-directory /private/morrow/new-manufacturing-seed --allow-write
```

This continuation verifies the saved target, principal, acknowledged ownership ledger and actual category paths before creating exactly eight products. It refuses repeat dispatch. Retain the private ledger after a failure; the command does not reset or clean a target.

## Complete the stages

| Stage | Operation company | Expected mapped result |
| --- | --- | --- |
| CATEGORIES | North | 9 categories created; 3 unchanged, including native Services |
| Seed existing products | Setup, after categories | Exactly 8 shared templates and their normal generated variants |
| PRODUCTS_NORTH | North | 12 products created; 4 products unchanged; 12 categories unchanged |
| PRODUCTS_SOUTH | South | 12 products created; MA-P-0010 updated; 3 products unchanged; 12 categories unchanged |
| PRODUCT_STATUS_NORTH | North | MA-P-0031 made inactive; 15 products and 12 categories unchanged |
| PRODUCT_STATUS_SOUTH | South | MA-P-0028 made inactive; 15 products and 12 categories unchanged |
| BOMS_NORTH | North | 3 headers, 12 lines and 6 operations created |
| BOMS_SOUTH | South | 3 headers, 12 lines and 6 operations created |

Product model counts are 16 per partition. The complete product workspace also maps the 12 category rows as UPSERT dependencies, so North totals 28 rows with 12 created and 16 unchanged; South totals 28 rows with 12 created, 1 updated and 15 unchanged. Reusing recursive category identities avoids relying on `complete_name`, whose storage differs between Odoo 19 and 20.

Initial product loads omit Active and use Odoo's normal true default. The intermediate answer key explicitly requires all 32 templates and generated variants to be active with their intended references. This ordering avoids a native Odoo 20 inactive-create case in which the internal reference can miss the generated variant. Use the same ordering on both supported majors. After both product partitions exist, run the two separate company-filtered status stages using the full saved product combination and category dependencies. Each status workspace has zero creates, one update and 27 unchanged rows. Only Active changes: MA-P-0031 for North and MA-P-0028 for South. Verify the unchanged final business answers before either BoM phase.

Use the normal application paths to freeze source files, save the product combination, map fields, prepare, review and approve, execute, reconcile and read back results. Reopen saved work before execution so in-memory setup cannot conceal missing persisted decisions. Save each reviewed phase as a Recipe and use the reordered delivery as a distinct source. Rebuilding the matching prerequisite target state is necessary to qualify initial Recipe creates and updates. On a completed target, repeats and reordered Recipe runs must create zero records, update zero mapped values, and report all mapped rows unchanged.

The required qualification matrix covers Odoo 19 and Odoo 20, LOCAL and REMOTE access, and authoring and Recipe runs. A controlled target checks orchestration and contracts. A live disposable target establishes observed Odoo behavior. Record those results separately and capture the observed Odoo release and source attestation.

## Resolve the deliberate ambiguities

Join `products_core.csv` and `product_details.csv` using CompanyCode and ProductRef, with exactly one row on each side. Join before the company filter and expect 32 combined rows. Core Name is the product name; details Name is a different export label and must remain inspectable without replacing it. The new delivery changes row order independently across files, so a positional join cannot satisfy the answer key.

Map ProductRef to the shared template's `default_code`, core Name to `name`, French decimal SalesPrice to `list_price`, and leave active unmapped during the initial load. In the later status phases, add StatusCode 10/20 as active true/false. Article means the captured Goods selection code `consu`; Service means `service` on both reviewed majors. CategoryKey links to the category dataset. Resolve PCE to existing Units. Split TagCodes on semicolons and replace `product_tag_ids` using exact tag names. Do not append stale tags. Preserve zero prices and include inactive records when looking up or reading back products.

CategoryKey is a source key. The native category identity is Name within its recursively identified parent; blank parent explicitly means a root. Three different paths end with Assembly. A leaf-name match must not collapse them. Source category Code and Active are not target writes in this baseline. Odoo 19 has no category `company_id`; Odoo 20 shared categories have it unset.

Use the supplied `bom_headers.csv` as six physical header records and `bom_rows.csv` as 24 physical component records. Select the chosen company in both. Header records are not derived from component rows at runtime. The raw component export deliberately retains repeated header cells so learners can inspect the original export shape. Static checks verify that every repeated value agrees with its separate supplied header, including exactly one Sequence = 10 component per BoM; that sequence does not select header records.

BOMRef maps to the BoM's `code`. It is globally unique within this practice namespace. CompanyCode selects rows; it is not a native company name or an identity alias. Assign `company_id` separately using the chosen company name and verify it after execution. FinishedProductRef resolves to `product.template.default_code`; do not infer the finished product from the BoM code. Map BaseQty directly to `product_qty` and use Manufacture (`normal`). A header is active only when both Approved and Active are Oui. HeaderName is source evidence; this baseline invents no target BoM name field.

For each line, Sequence within its BoM is the native key; LineRef remains a source trace key. ComponentRef resolves to the generated `product.product` variant by its reviewed default code. Parse French decimal Quantity directly into `product_qty`. Every baseline QuantitySeries is 1; BaseQty 1000 is not a multiplier. Other quantity-series rules need their own reviewed exercise. Never collapse the two MA-P-0011 rows in MA-BOM-003: their sequences are 30 and 40, with quantities 1.000 and 2.000.

Import `bom_operations.csv` at its own grain alongside the separate headers and lines. Joining operations onto component rows would multiply both: two operations and four lines would produce eight combinations for a single BoM. Name within the BoM is the reviewed operation key; Sequence is ordering. Resolve WorkCentreCode, set manual time mode, and parse Minutes into `time_cycle_manual`. Do not write operation company directly: it follows the BoM. Each BoM must retain Assemble at sequence 10 for 12.50 minutes and Inspect at sequence 20 for 3.25 minutes.

Both header and line unit fields are `product_uom_id` on Odoo 19 and `uom_id` on Odoo 20. Product templates use `uom_id` on both. Verify these fields from the captured target major rather than using an Odoo 19 alias by default.

Weight, ModifiedAtUS, ModifiedAtISO and ValidFromText remain source evidence. This exercise makes no stock target date or weight mapping claim. A later issue may add a separately reviewed mapping and answer key without rewriting this accepted baseline.

## Check the business result

The final target contains 12 distinct practice category paths, including the preserved native Services root, 32 shared templates, the required normal variants, 6 company-owned BoMs, 24 lines and 12 operations. Product prices total 1313.50: 661.00 for the North source partition and 652.50 for South. Component quantities total 22.500, and manual operation minutes total 94.50. Those controls supplement exact per-record assertions; they cannot detect every swapped relationship.

Check the two subassemblies explicitly: MA-BOM-001 includes product MA-P-0005, whose BoM is MA-BOM-005; MA-BOM-002 includes MA-P-0006, whose BoM is MA-BOM-006. Keep both repeated component lines in MA-BOM-003. Check exact work-centre ownership, template-versus-variant relationships, tag sets, inactive products MA-P-0028 and MA-P-0031, and zero-price products MA-P-0027 and MA-P-0032.

Use the manifest and the independent answer key when adding a regression. Give a new failure its own minimal source variant or named target setup, expected block or result, and affected matrix cases. Keep existing source bytes and accepted answers unchanged unless the business scenario itself is intentionally revised with a new version. Do not make the test pass by bypassing the normal application lifecycle or by replacing the oracle with an execution snapshot.

## Run and interpret the checks

Run from the checkout root with its test dependencies installed. For an isolated controlled journey:

```sh
python -m scripts.qualify_practice_manufacturing \
  --controlled --expected-major 20 --mode LOCAL --entrypoint authoring \
  --output-directory /tmp/new-morrow-manufacturing-check
```

Select `--entrypoint recipe` for the distinct reordered delivery. A live run replaces `--controlled` with the allocated `--target-descriptor` and explicit `--allow-write`. Select the descriptor's exact major and mode. The runner verifies source hashes before target access, loads each stage through normal services, reopens persisted work, reconciles actual values and checks an unchanged repeat. It starts no Impodo service or browser and retains the disposable target.

Use the run's evidence directory to distinguish controlled checks from live
Odoo, browser, and host qualification. The staged exercise creates BoM headers
before applying their final active state. A direct inactive create is a
separate Odoo behavior.
