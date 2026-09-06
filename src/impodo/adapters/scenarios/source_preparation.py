"""Build pinned combined lookup tables before scenario profile preparation.

Migration stages: source preparation. Layer: adapter.

The contract is intentionally narrow: select one column from each reviewed
CSV/XLSX fixture, evaluate one bounded scalar formula, and retain the first
occurrence of each result. It is sufficient for source-owned lookup tables
such as units of measure without allowing arbitrary code or hidden target
seeding.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from impodo.application.data_version.source_files import (
    MAX_SOURCE_ROWS,
    load_selected_source_table,
    load_source_tables,
)
from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.preparation.source import (
    PreparedBundle,
    SourceLoadError,
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from impodo.domain.recipe.value_rules import evaluate_formula
from impodo.domain.scenarios import CombinedDistinctTable
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import portable_value


def prepare_scenario_sources(
    plan: CompiledMigrationPlan,
    fixture_directory: str | Path,
    combined_tables: tuple[CombinedDistinctTable, ...],
) -> PreparedBundle:
    """Prepare physical profile tables plus declared combined distinct tables."""

    root = Path(fixture_directory).resolve(strict=True)
    physical_tables = load_source_tables(plan, root)
    by_dataset = {table.dataset: table for table in physical_tables}
    source_hashes = {
        _relative_source_name(root, table.path): table.content_hash
        for table in physical_tables
    }

    for rule in combined_tables:
        if rule.output_dataset not in by_dataset:
            raise SourceLoadError(
                "combined output_dataset does not exist in the compiled profile"
            )
        table, input_hashes = _combined_distinct_table(root, rule)
        by_dataset[rule.output_dataset] = table
        source_hashes.update(input_hashes)
        source_hashes[f"derived/{rule.output_dataset}"] = table.content_hash

    return prepare_source_tables(
        plan,
        tuple(by_dataset[item.name] for item in plan.datasets),
        source_hashes=source_hashes,
    )


def _combined_distinct_table(
    root: Path,
    rule: CombinedDistinctTable,
) -> tuple[SourceTable, dict[str, str]]:
    values: list[Any] = []
    seen: set[str] = set()
    source_hashes: dict[str, str] = {}
    input_rows = 0

    for index, selected in enumerate(rule.inputs, 1):
        path = _contained_fixture_path(root, selected.file)
        table_key = (
            f"sheet:{selected.sheet}"
            if path.suffix.casefold() == ".xlsx"
            else "csv"
        )
        table = load_selected_source_table(
            path,
            dataset=f"{rule.output_dataset}_input_{index}",
            table_key=table_key,
            encoding=(selected.encoding if table_key == "csv" else None),
            delimiter=(selected.delimiter if table_key == "csv" else None),
            header_row=selected.header_row,
            source_display_name=selected.file,
        )
        if selected.field not in table.headers:
            raise SourceLoadError("combined input field is missing")
        input_rows += len(table.rows)
        if input_rows > MAX_SOURCE_ROWS:
            raise SourceLoadError(
                f"combined inputs exceed {MAX_SOURCE_ROWS} source rows"
            )
        source_hashes[selected.file] = table.content_hash
        for row in table.rows:
            try:
                value = evaluate_formula(
                    rule.formula,
                    {"value": row.values.get(selected.field)},
                )
            except (ArithmeticError, KeyError, TypeError, ValueError) as error:
                raise SourceLoadError(
                    "combined source formula could not produce a value"
                ) from error
            if value is None or (isinstance(value, str) and not value.strip()):
                raise SourceLoadError("combined source value is blank")
            key = content_hash(portable_value(value))
            if key not in seen:
                seen.add(key)
                values.append(value)

    rule_payload = rule.model_dump(mode="json")
    derived_hash = content_hash(
        {
            "rule": rule_payload,
            "inputs": dict(sorted(source_hashes.items())),
            "values": [portable_value(value) for value in values],
        }
    )
    return (
        SourceTable(
            dataset=rule.output_dataset,
            path=PurePosixPath(f"derived/{rule.output_dataset}"),
            headers=(rule.output_field,),
            rows=tuple(
                SourceRow(number=index, values={rule.output_field: value})
                for index, value in enumerate(values, 1)
            ),
            content_hash=derived_hash,
        ),
        source_hashes,
    )


def _contained_fixture_path(root: Path, relative_name: str) -> Path:
    candidate = root / relative_name
    if candidate.is_symlink():
        raise SourceLoadError("combined fixture input cannot be a symbolic link")
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise SourceLoadError("combined fixture input is unavailable") from error
    if not path.is_file():
        raise SourceLoadError("combined fixture input is not a file")
    return path


def _relative_source_name(root: Path, path: object) -> str:
    try:
        return Path(path).resolve(strict=True).relative_to(root).as_posix()
    except (OSError, TypeError, ValueError) as error:
        raise SourceLoadError("profile source path left the fixture directory") from error
