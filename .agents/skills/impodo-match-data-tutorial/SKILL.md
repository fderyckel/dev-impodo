---
name: impodo-match-data-tutorial
description: Create or revise a data-manager-friendly Impodo Match data tutorial. Use when documenting matching rules, field-value choices, relationships, transformations, edge cases, matching review workbooks, or Match data screenshots in docs/user/tutorials.
---

# Impodo Match Data Tutorial

## Overview

Create a concise Q&A tutorial that helps a data manager choose, review, and
confirm current Match data rules without needing implementation knowledge.
Use fictional examples and current browser screenshots.

## Establish the current product boundary

1. Use `impodo-documentation` first. Read `docs/style-guide.md`, inspect the
   working tree, and preserve unrelated work.
2. Read the `match` stage in `docs/workflow.yml`, the paired user and developer
   pages, and the browser template or focused tests for every rule described.
3. Treat the implemented browser labels and contracts as the authority. Put an
   unavailable idea in a clearly labelled **Not yet available** answer and link
   to its plan; never describe it as a current control.
4. Reuse a current, fictional PNG from `docs/images/user/` only when it shows
   the documented decision and labels. Otherwise recapture the authenticated
   browser at 1440 by 1024 with isolated fictional data.

## Write the tutorial

1. State the data manager's goal, what must already be confirmed, and what
   Match data changes. Explain that it saves rules and evidence; it does not
   alter accepted source data or write to Odoo.
2. Use question headings that a data manager would ask. Answer each with the
   exact browser control, a short fictional example, a clear next action, and
   an edge case where one matters.
3. Use a small coloured visual language consistently: 🟢 normal path, 🔵 useful
   check, 🟡 review before continuing, and 🔴 stop and resolve. Do not rely on
   colour alone; write the meaning as well.
4. Place a screenshot directly after the question it supports. Give it useful
   alternative text and a caption that tells the reader what to notice.
5. Cover the current rule families that apply to the requested tutorial:
   target mode and identity; scalar value providers; value types and
   transformations; Odoo choice matching and ordered conditional rules;
   linked-record origins and relationship kinds; review/support choices;
   known totals and business checks; captured-Odoo update approval; and the
   Save, Check, review-workbook, optional effect-preview, Confirm lifecycle.
6. Finish with a compact edge-case desk and a hand-off checklist. Keep internal
   symbols, hashes, transport, and code details out of the normal path.

## Check the result

1. Check every screenshot link, paired developer link, and tutorial index link.
2. Run the repository documentation checks and `git diff --check`.
3. State if a current browser capture could not be refreshed, or if a planned
   rule was deliberately kept out of the current tutorial.
