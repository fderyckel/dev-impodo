# Trial your files through Impodo

## What is available now

A technical operator can run repeatable Contact, Product, and bill-of-material
trials from fictional files to a disposable Odoo 19 database. The trial checks
the source, compares it with Odoo, loads it, reads the saved records back,
checks them against a separately reviewed expected result, and compares them
again. A successful repeat comparison must say that every row already matches
and that no further write is proposed.

The Product trial can also build a lookup dataset before matching. In the
current example, Impodo combines the Unit columns from the Product and
bill-of-material files, changes both `G` and `g` to `g`, removes duplicates,
and treats `PCE` as an ordinary source row. If `PCE` is missing from a
disposable target, the trial creates it through the normal load. If it already
exists, the row must compare as unchanged.

The trial is deliberately separate from customer work. It accepts only a
database reserved for scenarios, requires an explicit confirmation, and
retains a journal before sending a write. If the write outcome is uncertain,
it stops instead of trying again blindly.

![The normal browser load confirmation remains the data-manager path; the current scenario runner is a separate technical-operator command.](../../images/user/17b-load-confirmation.png)

## What to provide

For a new file-based trial, give the technical operator:

- the CSV or XLSX files to test;
- the reviewed field and relationship rules, currently expressed as an expert
  profile;
- the expected row totals before loading;
- a small, independently reviewed JSON example of the records that should
  exist afterward; and
- confirmation that the target database is disposable and starts in the
  expected state.

Do not put an Odoo key in any of those files. The operator supplies it through
a separate private key file.

## Current limits

The automated Contact round trip and the two-stage Product and
bill-of-material round trip are current. An offline Contact canary is also
included for quick checks that never contact Odoo. The Product example first
loads Units, Products, and bill-of-material headers. It then loads the lines so
each line can resolve the Product variant that Odoo created from its Product.

The command accepts a remote HTTPS target only when the scenario pins that
target's non-secret identity hash. The edu-ucaps Product and bill-of-material
definitions are registered, but their live result is not yet qualified.

An Odoo source, Odoo-to-Odoo trials, automatic database setup and cleanup,
scheduled background runs, and a trial of every browser page remain planned.
You can still run the normal browser workflow for supported migration shapes,
but the scenario command does not yet prove that entire browser journey.

For operator commands and evidence handling, use the
[scenario qualification runbook](../../developer/runbooks/scenario-qualification.md).
