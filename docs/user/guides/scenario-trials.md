# Trial your files through Impodo

## What is available now

A technical operator can run a repeatable Contact trial from a small set of
fictional files to a disposable Odoo 19 database on the same computer. The
trial checks the source, compares it with Odoo, loads it, reads the saved
record back, checks it against a separately reviewed expected result, and
compares it again. A successful repeat comparison must say that every row
already matches and that no further write is proposed.

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

The automated Contact round trip is current. An offline Contact canary is also
included for quick checks that never contact Odoo.

Product and bill-of-material files, an Odoo source, Odoo-to-Odoo trials,
remote targets, automatic database setup and cleanup, scheduled background
runs, and a trial of every browser page remain planned. You can still run the
normal browser workflow for those supported migration shapes, but the new
scenario command does not yet prove that entire browser journey.

For operator commands and evidence handling, use the
[scenario qualification runbook](../../developer/runbooks/scenario-qualification.md).
