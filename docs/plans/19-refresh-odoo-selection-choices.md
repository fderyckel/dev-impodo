# Refresh language choices in Match data

**Status:** Implemented on 2026-11-02.

The [Match data user workflow](../user/workflow/03-match-data.md#refresh-available-languages)
now owns the refresh and rematching instructions. The
[developer workflow](../developer/workflow/03-match-data.md#refresh-selection-choices)
owns candidate bindings, atomic publication, local database scopes and focused
verification. Recipe runs use the same capability through their target-value
review, without changing the pinned Recipe version.

The original proposal has been reconciled into those current owners. Live
Odoo qualification is separate from the isolated route and browser regression
evidence; no customer workspace was changed during implementation checks.
