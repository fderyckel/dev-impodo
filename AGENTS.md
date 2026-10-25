# Impodo repository instructions

These instructions apply to the complete repository.

## Protect behavior first

- Treat accepted contracts, runtime behavior, and focused tests as the semantic
  authority. A lint or type-checking suggestion does not authorize a behavior
  change.
- Preserve evidence hashes, portable identities, authorization, idempotency,
  transaction boundaries, recovery behavior, and deterministic ordering.
- Before changing a browser route, service orchestration, repository, or
  background worker, apply the `impodo-duckdb-performance` skill when it is
  available. Keep local database work inside one bounded synchronous operation,
  and never introduce a process-wide or cross-request DuckDB connection cache.
- Preserve unrelated worktree edits. If another change overlaps the same lines,
  reconcile the current file before editing it.

## Enforce Python quality

- Read
  [`docs/developer/reference/python-code-quality.md`](docs/developer/reference/python-code-quality.md)
  before adding, editing, reviewing, or refactoring Python.
- Apply the `impodo-python-quality` skill when it is available.
- Run the repository quality command before handing off any Python change:

  ```powershell
  uv run --frozen --group quality python scripts/check_python_quality.py
  ```

- Ruff owns imports, modernization, and non-type lint rules. Basedpyright owns
  type compatibility, narrowing, annotations, and interface conformance.
- Keep every Basedpyright diagnostic visible. Do not create a baseline or run
  `basedpyright --writebaseline`; correct the type contract and verify the
  affected behavior instead.
- Do not add blanket `noqa`, `type: ignore`, package exclusions, or disabled
  diagnostic categories. A narrow suppression requires a documented reason and
  must stay beside the affected expression.
- Apply automated Ruff fixes only when they are marked safe and the resulting
  diff stays within the approved scope. Review unsafe fixes individually and
  run focused behavioral tests.
- Do not run repository-wide formatting as part of an unrelated change. Format
  a new file or a deliberately approved file only, then inspect its complete
  diff.

## Verify the affected behavior

- Run focused tests for every changed responsibility in addition to the static
  quality command.
- Apply the `impodo-documentation` skill to documentation changes when it is
  available. Always run the repository documentation checks after a
  documentation edit.
- Report checks that passed, checks that failed, and checks that were omitted.
  Do not describe an unavailable executable or an unrelated failing test as a
  clean result.
