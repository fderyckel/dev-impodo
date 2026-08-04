# CLI and operating model

## Operating principle

The CLI separates target capture from offline preflight. This makes the live
boundary visible to operators and allows exactly the same saved evidence to be
reviewed or rerun without reconnecting to Odoo.

The installed executable is `impodo-cli`. The equivalent development form is
`PYTHONPATH=src .venv/bin/python -m impodo`.

## Configuration

The CLI reads:

- `IMPODO_ODOO_BASE_URL`;
- `IMPODO_ODOO_DATABASE`;
- `IMPODO_ODOO_API_KEY`;
- optional `IMPODO_ODOO_TIMEOUT_SECONDS`;
- optional `IMPODO_ODOO_PAGE_SIZE`.

The API key is loaded from an environment variable. It is never a command-line
flag because process listings and shell histories can expose arguments. A
separate secret-provider integration is not implemented.

The connector derives `LOCAL` from a literal `127.0.0.1` or `::1` URL. Every
other target is `REMOTE` and must use HTTPS. `Json2Config` also supports a
context and relevant-module list when constructed in Python, but the CLI
environment loader does not expose those two settings.

## Commands

### Prepare and validate sources

```bash
impodo-cli profile \
  --profile profiles/examples/bom.yaml \
  --input examples/bom \
  --output build/bom-profile/prepared-records.json
```

Performs strict profile validation and typed source preparation. It does not
contact Odoo. The input directory may contain profile-declared `.csv` and
`.xlsx` files. XLSX profiles must select a worksheet explicitly; see
[Profile authoring](profile-authoring.md).

### Capture target metadata

```bash
impodo-cli snapshot-metadata \
  --profile profiles/examples/golden_slice.yaml \
  --connector json2 \
  --output snapshots/run-20260728/target-metadata.json
```

Behavior:

- loads the exact target configuration;
- fingerprints the target;
- requests only profile-required model metadata;
- writes canonical JSON atomically;
- prints the output path.

Full model/field compatibility validation occurs in `preflight`, when the
metadata snapshot is evaluated together with the prepared records.

### Capture target records

```bash
impodo-cli snapshot-records \
  --profile profiles/examples/golden_slice.yaml \
  --input examples/golden \
  --connector json2 \
  --output snapshots/run-20260728/dev-records.json
```

The source is needed to derive bounded identity domains. The command prepares
natural keys but does not classify records. It compiles the record requests,
retrieves only required models and fields, verifies live pagination
completeness, and writes the snapshot atomically. Metadata and record
fingerprints are compared later by `preflight`.

### Run offline preflight

```bash
impodo-cli preflight \
  --profile profiles/examples/golden_slice.yaml \
  --input examples/golden \
  --metadata snapshots/run-20260728/dev-metadata.json \
  --records snapshots/run-20260728/dev-records.json \
  --output runs/run-20260728
```

Outputs:

```text
runs/run-20260728/
├── impodo_preflight_manifest.json
└── impodo_preflight_report.xlsx
```

The command verifies matching metadata/record fingerprints, profile bindings
when present, record source hashes when present, and record completeness. It
then runs preparation, metadata validation, resolution, matching, comparison,
and classification. The workbook is generated from the canonical JSON
manifest.

It makes no network call.

Add `--preview-dir build/previews` to render one PNG per workbook sheet for
visual verification. Preview generation uses the same workbook runtime and is
optional.

There is no one-shot live command. The explicit capture/offline separation is
intentional.

### Synthetic benchmark

```bash
impodo-cli benchmark --rows 360000
```

This measures an in-memory identity dictionary only. It is not an end-to-end
performance or memory test and has no pass/fail timing threshold.

## Console behavior

Successful preflight prints a bounded summary:

```text
CREATE 42 | UPDATE 18 | UNCHANGED 51 | AMBIGUOUS 2 | BLOCKED 7
Manifest: runs/run-20260728/impodo_preflight_manifest.json
Review workbook: runs/run-20260728/impodo_preflight_report.xlsx
Semantic hash: sha256:…
```

Console output does not print credentials, full records, raw failed cells, or
numeric Odoo IDs.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Command completed and artifacts are valid; row-level blocked or ambiguous outcomes may exist |
| `2` | CLI usage or configuration error |
| `3` | Profile or source package structural/security validation failed |
| `4` | Connector, authentication, authorization, or transport failure |
| `6` | Manifest/workbook report generation failed |

Row-level `BLOCKED` and `AMBIGUOUS` are expected review outcomes, not process
failures. Automation may apply a separate policy to reject a run whose summary
contains either.

Argument parsing also exits with argparse's standard code `2`. The CLI maps
profile, source, and local value errors to `3`; connector errors to `4`; and
workbook/report errors to `6`.

## Safe write behavior

- Parent directories are created when needed.
- Prefer a new run directory for each reviewed result.
- JSON files are written to a `.partial` sibling and atomically renamed.
- Workbook creation uses a temporary working directory.
- The manifest is finalized before workbook generation. If workbook creation
  fails, the manifest can remain by itself; treat the directory as incomplete
  until both required output files exist.

## Operator runbook

1. Confirm the source package and profile intended for review.
2. Confirm the exact URL and database are authorised for read-only inspection.
3. Validate the profile offline.
4. Capture metadata and address any model/field mismatch.
5. Capture records and confirm completeness.
6. Run offline preflight.
7. Verify the target fingerprint and input hashes on `Target`.
8. Review `Blocked Records` and `Ambiguous Matches` first.
9. Review proposed updates with `Field Differences`.
10. Reconcile summary counts with source expectations.
11. Retain the complete run directory according to the data policy.

No step in this runbook authorizes or performs an Odoo write.

## Staleness

A result describes the target snapshot timestamp, not the current target.
Operators must recapture records when:

- source or profile content changes;
- the target database is restored, upgraded, or reconfigured;
- relevant modules change;
- target data may have changed since review;
- the requirements plan changes;
- a snapshot integrity check fails.

The proof of concept displays the snapshot timestamp prominently but does not
invent a universal maximum age; that is an operational policy decision.

## Credentials and logs

The connector reads `IMPODO_ODOO_BASE_URL`, `IMPODO_ODOO_DATABASE`,
`IMPODO_ODOO_API_KEY`, `IMPODO_ODOO_TIMEOUT_SECONDS`, and
`IMPODO_ODOO_PAGE_SIZE`. Regardless of names:

- the API key is excluded from configuration representations and errors;
- HTTP response bodies are not included in errors;
- exception text is sanitized;
- shell examples never inline secrets;
- successful commands print bounded counts, output paths, and the semantic
  hash; there is no separate structured logging subsystem.
