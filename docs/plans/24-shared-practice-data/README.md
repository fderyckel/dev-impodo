---
audience: developer
kind: plan
status: proposed
---

# Remaining extensions to the Morrow practice data

The canonical [customer pack](../../../scenarios/impodo-practice/v1/README.md)
and [manufacturing pack](../../../scenarios/impodo-practice/manufacturing/v1/README.md)
own their source deliveries, independent business answers and target starting
states. This proposal references those owners. It contains no copied customer,
category, product or manufacturing CSVs and no duplicate baseline oracle.

Two local CSVs remain: eight account rows and a six-row customer phone patch.
Their fourteen physical rows support review of unimplemented extensions.
The eighteen fault specifications and supplier-repair overlay also remain
proposals; a specification is not a runtime test or a published Recipe.

| Local proposed input | Rows | Business decision |
| --- | ---: | --- |
| [accounts.csv](deliveries/2026-04-15/accounts.csv) | 8 | Preserve leading-zero codes and review exact company/code/parent fields on each Odoo major. |
| [customer_phone_patch.csv](patches/2026-05-20/customer_phone_patch.csv) | 6 | Change only Phone on existing NORTH contacts, with zero creates. |

## Use the canonical owners

Customer enrichment, staged payment terms and the immediate-term dependency
regression use the customer owner. Its runner maps the documented customer
fields; optional VAT, Country, Language and chain mappings remain distinct
proposals. A new Italian label cannot establish a Language blocker when
Language is not mapped.

Supplied categories, combined products, separate BoM headers, component lines
and operations use the manufacturing owner. Its two deliveries contain twelve
CSVs and 236 physical rows. The original five baseline files retain their
bytes; the separate six-row header export declares a different input grain.
Native Services is an existing unowned reference, not a fixture-created
category to adopt or delete. The canonical seed and oracle own that decision
and the category phase's nine creates and three unchanged records.

Use each owner's recorded qualification evidence for the exact Odoo major,
access mode and entrypoint that ran. A customer success does not qualify a
manufacturing load, and a controlled REMOTE result does not establish live
HTTPS behavior or a Linux deployment.

## Keep the split and generated hierarchy proposals separate

P05 still proposes deriving headers from repeated BoM rows. The current
manufacturing exercises instead consume physical header, line and operation
exports. They establish no related-split attribute propagation or child
categorical provider persistence through Recipe. Those paths need a completed
normal application provider and their own executable cases before promotion.

P09 still proposes deriving category paths from product columns in a separate
workspace. Its expected category paths refer to the canonical manufacturing
answer key. Supplied and generated categories are alternatives; loading both
must not create duplicate paths. The optional customer-chain relationship
requires an appropriate captured target field.

The optional product date preview also remains proposed. Current manufacturing
rules map no date or weight target field. Native Excel cells require a real
workbook case; CSV timestamp text cannot prove that behavior.

## Review patch, repair and accounts intent

P07 applies the six-row local phone patch after the selected canonical NORTH
contacts exist. Phone is the only write field. Require zero creates and
preserve the actual other target values.

P10 uses the [supplier-repair overlay](target-overlays/vendor-term-repair.json)
as a separate target branch. It selects MA-N-0004 from the canonical customer
file, assigns the supplier term and explicitly clears the mistaken customer
term. MA-N-0002 retains its legitimate customer term. The overlay is not a
current runner or a qualification receipt.

P08 requires captured account fields and reviewed company and business-key
semantics for each Odoo major. Source ParentCode is business intent; it does
not promise a standard account hierarchy field. No localization, chart of
accounts or tax correctness is asserted by these invented labels.

## Review fault specifications against their actual source

The eighteen JSON files in [faults](faults/) describe keyed source changes or
controlled runtime setups. Customer and manufacturing source mutations refer
to their canonical owner; the patch mutation refers to its local proposal.
All paths in this metadata resolve from this folder, including JSON files
inside faults/ and target-overlays/.

Apply a mutation to a copy and record its resulting hash. The current main
manufacturing rule maps Quantity directly with QuantitySeries fixed at one.
Changing that unmapped series to zero does not automatically create a blocker.
F09 needs an explicit reviewed divisor rule or business check before its safe
failure can be qualified. Runtime permissions, partial target lists and lost
write replies likewise need executable controlled adapters.

## Inspect ownership and history

- [exercise-contracts.json](exercise-contracts.json) points current subsets to their canonical contracts and retains only the additional proposal decisions.
- [expected-business-results.json](expected-business-results.json) retains remaining proposal answers without copying customer or manufacturing baseline values.
- [target-seed-projection.json](target-seed-projection.json) retains account, patch and supplier-repair prerequisites; current seeds belong to their canonical owners.
- [data-dictionary.json](data-dictionary.json) describes the two local file grains and synthetic provenance.
- [manifest.json](manifest.json) pins all remaining local proposal assets.
- [validation-evidence.json](validation-evidence.json) records fourteen-row static checks and omitted work.
- The reports in [history](history/) retain the original 250-row review and the later 126-row review as dated history.

Account, phone-patch, supplier-repair, generated-hierarchy, split-provider and
runtime fault qualification remain unexecuted by this proposal. Preserve
legacy workbook scenarios, golden comparisons, compatibility, serialization,
scale and current tutorial fixtures until their own consumers have verified
replacement coverage.
