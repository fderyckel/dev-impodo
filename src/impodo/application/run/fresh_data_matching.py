"""Match selected Recipe inputs to one fresh Test delivery deterministically."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from impodo.domain.serialization import content_hash
from impodo.application.data_version.inspection import SourceFileCatalog
from impodo.domain.recipe.source_binding import (
    logical_dataset_storage_name,
    normalize_recipe_source_name,
)


@dataclass(frozen=True, slots=True)
class FreshDataInputRequirement:
    """Describe one logical source table that a Recipe expects."""

    logical_dataset_id: str
    label: str
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FreshDataParameterRequirement:
    """Describe one value supplied by this run rather than by source rows."""

    logical_parameter_id: str
    label: str
    value_type: str
    required: bool
    constraints: Mapping[str, object]
    supplied_value: str | None


@dataclass(frozen=True, slots=True)
class FreshDataRecipeRequirement:
    """Present the source contract of one exact selected Recipe revision."""

    recipe_id: str
    recipe_revision: int
    display_name: str
    business_purpose: str
    inputs: tuple[FreshDataInputRequirement, ...]
    parameters: tuple[FreshDataParameterRequirement, ...]


class FreshDataMatchStatus(StrEnum):
    """Describe whether one Recipe input has a safe physical table choice."""

    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class FreshDataTableCandidate:
    """Present one detected physical table that satisfies an input shape."""

    candidate_id: str
    file_id: str
    file_name: str
    table_key: str
    table_name: str
    table_kind: str
    worksheet_name: str
    row_count: int
    columns: tuple[str, ...]
    warnings: tuple[str, ...]
    name_matches: bool

    @property
    def display_name(self) -> str:
        if self.table_key == "csv":
            return self.file_name
        return f"{self.file_name} / {self.table_name}"


@dataclass(frozen=True, slots=True)
class FreshDataInputMatch:
    """Explain one Recipe logical input and its current physical match."""

    logical_dataset_id: str
    label: str
    dataset_name: str
    columns: tuple[str, ...]
    recipe_names: tuple[str, ...]
    status: FreshDataMatchStatus
    candidates: tuple[FreshDataTableCandidate, ...]
    selected_candidate_id: str | None
    explanation: str

    @property
    def selected_candidate(self) -> FreshDataTableCandidate | None:
        return next(
            (
                item
                for item in self.candidates
                if item.candidate_id == self.selected_candidate_id
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class FreshDataMatchPlan:
    """Hold the complete explainable table match for one fresh delivery."""

    inputs: tuple[FreshDataInputMatch, ...]
    unused_files: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ready_to_accept(self) -> bool:
        return bool(self.inputs) and all(
            item.status is FreshDataMatchStatus.MATCHED for item in self.inputs
        ) and not self.unused_files

    @property
    def can_submit(self) -> bool:
        return bool(self.inputs) and all(
            item.status
            in {FreshDataMatchStatus.MATCHED, FreshDataMatchStatus.AMBIGUOUS}
            for item in self.inputs
        ) and not self.unused_files

    @property
    def needs_choice(self) -> bool:
        return any(
            item.status is FreshDataMatchStatus.AMBIGUOUS for item in self.inputs
        )


@dataclass(slots=True)
class _FreshDataInputDefinition:
    columns: tuple[str, ...]
    dataset_name: str
    label: str
    recipe_names: list[str]
    signature: tuple[str, ...]


def build_fresh_data_match_plan(
    requirements: tuple[FreshDataRecipeRequirement, ...],
    catalogs: tuple[SourceFileCatalog, ...],
    *,
    overrides: Mapping[str, str] | None = None,
) -> FreshDataMatchPlan:
    """Match Recipe logical inputs to bounded detected-table evidence."""

    selected_overrides = dict(overrides or {})
    definitions: dict[str, _FreshDataInputDefinition] = {}
    conflicts: dict[str, str] = {}
    storage_owners: dict[str, str] = {}
    for recipe in requirements:
        for source_input in recipe.inputs:
            logical_id = source_input.logical_dataset_id
            signature = tuple(
                sorted(
                    normalize_recipe_source_name(column)
                    for column in source_input.columns
                )
            )
            current = definitions.get(logical_id)
            if current is None:
                dataset_name = logical_dataset_storage_name(logical_id)
                owner = storage_owners.get(dataset_name)
                if owner is not None and owner != logical_id:
                    conflicts[logical_id] = (
                        "Two Recipe inputs resolve to the same accepted table name"
                    )
                    conflicts[owner] = conflicts[logical_id]
                storage_owners[dataset_name] = logical_id
                definitions[logical_id] = _FreshDataInputDefinition(
                    columns=tuple(source_input.columns),
                    dataset_name=dataset_name,
                    label=source_input.label,
                    recipe_names=[recipe.display_name],
                    signature=signature,
                )
            elif current.signature != signature:
                conflicts[logical_id] = (
                    "Selected Recipes disagree about this logical source input"
                )
            elif recipe.display_name not in current.recipe_names:
                current.recipe_names.append(recipe.display_name)

    matches: list[FreshDataInputMatch] = []
    explicitly_selected: set[str] = set()
    for logical_id, definition in definitions.items():
        candidates = _fresh_table_candidates(
            catalogs,
            label=definition.label,
            required_columns=definition.columns,
        )
        if logical_id in conflicts:
            matches.append(
                FreshDataInputMatch(
                    logical_dataset_id=logical_id,
                    label=definition.label,
                    dataset_name=definition.dataset_name,
                    columns=definition.columns,
                    recipe_names=tuple(definition.recipe_names),
                    status=FreshDataMatchStatus.CONFLICT,
                    candidates=candidates,
                    selected_candidate_id=None,
                    explanation=conflicts[logical_id],
                )
            )
            continue

        override = selected_overrides.get(logical_id, "").strip()
        selected: FreshDataTableCandidate | None = None
        status = FreshDataMatchStatus.MISSING
        explanation = "No safe detected table contains every required column."
        if override:
            explicitly_selected.add(logical_id)
            selected = next(
                (item for item in candidates if item.candidate_id == override),
                None,
            )
            if selected is None:
                status = FreshDataMatchStatus.CONFLICT
                explanation = (
                    "The selected table is no longer a current compatible choice."
                )
            else:
                status = FreshDataMatchStatus.MATCHED
                explanation = "You selected this table from the compatible choices."
        elif len(candidates) == 1:
            selected = candidates[0]
            status = FreshDataMatchStatus.MATCHED
            explanation = (
                "All required columns were found in the only compatible table."
            )
        elif candidates:
            name_matches = tuple(item for item in candidates if item.name_matches)
            if len(name_matches) == 1:
                selected = name_matches[0]
                status = FreshDataMatchStatus.MATCHED
                explanation = (
                    "All required columns were found and the table name matches "
                    "the Recipe input."
                )
            else:
                status = FreshDataMatchStatus.AMBIGUOUS
                explanation = (
                    "More than one detected table contains every required column."
                )
        matches.append(
            FreshDataInputMatch(
                logical_dataset_id=logical_id,
                label=definition.label,
                dataset_name=definition.dataset_name,
                columns=definition.columns,
                recipe_names=tuple(definition.recipe_names),
                status=status,
                candidates=candidates,
                selected_candidate_id=(
                    selected.candidate_id if selected is not None else None
                ),
                explanation=explanation,
            )
        )

    chosen: dict[str, list[int]] = {}
    for index, match in enumerate(matches):
        if match.selected_candidate_id is not None:
            chosen.setdefault(match.selected_candidate_id, []).append(index)
    for indexes in chosen.values():
        if len(indexes) < 2:
            continue
        for index in indexes:
            match = matches[index]
            can_choose_another = len(match.candidates) > 1
            matches[index] = replace(
                match,
                status=(
                    FreshDataMatchStatus.AMBIGUOUS
                    if can_choose_another
                    else FreshDataMatchStatus.CONFLICT
                ),
                selected_candidate_id=(
                    match.selected_candidate_id
                    if match.logical_dataset_id in explicitly_selected
                    else None
                ),
                explanation=(
                    "One physical table cannot fill two different Recipe inputs. "
                    + (
                        "Choose another compatible table."
                        if can_choose_another
                        else "Add a separate table for one of these inputs."
                    )
                ),
            )

    overlapping_indexes: set[int] = set()
    for left_index, left in enumerate(matches):
        left_candidate = left.selected_candidate
        if left_candidate is None:
            continue
        for right_index in range(left_index + 1, len(matches)):
            right_candidate = matches[right_index].selected_candidate
            if right_candidate is not None and _fresh_candidates_overlap(
                left_candidate,
                right_candidate,
            ):
                overlapping_indexes.update((left_index, right_index))
    for index in overlapping_indexes:
        match = matches[index]
        can_choose_another = len(match.candidates) > 1
        matches[index] = replace(
            match,
            status=(
                FreshDataMatchStatus.AMBIGUOUS
                if can_choose_another
                else FreshDataMatchStatus.CONFLICT
            ),
            selected_candidate_id=(
                match.selected_candidate_id
                if match.logical_dataset_id in explicitly_selected
                else None
            ),
            explanation=(
                "A worksheet and one of its Excel tables cover the same workbook "
                "area. "
                + (
                    "Choose a non-overlapping table."
                    if can_choose_another
                    else "Supply separate tables for these Recipe inputs."
                )
            ),
        )

    resolved = all(
        item.status is FreshDataMatchStatus.MATCHED for item in matches
    )
    relevant_file_ids = (
        {
            item.selected_candidate.file_id
            for item in matches
            if item.selected_candidate is not None
        }
        if resolved
        else {
            candidate.file_id
            for item in matches
            for candidate in item.candidates
        }
    )
    unused_files = tuple(
        catalog.display_name
        for catalog in catalogs
        if catalog.file_id not in relevant_file_ids
    )
    warnings = tuple(
        dict.fromkeys(
            warning
            for item in matches
            for candidate in (
                (item.selected_candidate,)
                if item.selected_candidate is not None
                else item.candidates
            )
            if candidate is not None
            for warning in candidate.warnings
        )
    )
    return FreshDataMatchPlan(
        inputs=tuple(matches),
        unused_files=unused_files,
        warnings=warnings,
    )


def _fresh_table_candidates(
    catalogs: tuple[SourceFileCatalog, ...],
    *,
    label: str,
    required_columns: tuple[str, ...],
) -> tuple[FreshDataTableCandidate, ...]:
    required_tokens = tuple(
        normalize_recipe_source_name(item) for item in required_columns
    )
    if (
        not required_tokens
        or any(not token for token in required_tokens)
        or len(set(required_tokens)) != len(required_tokens)
    ):
        return ()
    candidates = []
    for catalog in catalogs:
        for table in catalog.tables:
            if table.formula_cell_count or table.error_cell_count:
                continue
            columns_by_token: dict[str, list[str]] = {}
            for column in table.columns:
                columns_by_token.setdefault(
                    normalize_recipe_source_name(column.name),
                    [],
                ).append(column.name)
            if any(
                len(columns_by_token.get(token, ())) != 1
                for token in required_tokens
            ):
                continue
            candidate_id = content_hash(
                {
                    "catalog_hash": catalog.content_hash,
                    "file_id": catalog.file_id,
                    "table_key": table.table_key,
                }
            )
            name_token = normalize_recipe_source_name(label)
            candidates.append(
                FreshDataTableCandidate(
                    candidate_id=candidate_id,
                    file_id=catalog.file_id,
                    file_name=catalog.display_name,
                    table_key=table.table_key,
                    table_name=table.name,
                    table_kind=table.kind,
                    worksheet_name=table.worksheet_name,
                    row_count=table.row_count,
                    columns=tuple(column.name for column in table.columns),
                    warnings=tuple(
                        dict.fromkeys((*catalog.warnings, *table.warnings))
                    ),
                    name_matches=(
                        normalize_recipe_source_name(table.name) == name_token
                        or normalize_recipe_source_name(
                            Path(catalog.display_name).stem
                        )
                        == name_token
                    ),
                )
            )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (item.display_name.casefold(), item.candidate_id),
        )
    )


def _fresh_candidates_overlap(
    left: FreshDataTableCandidate,
    right: FreshDataTableCandidate,
) -> bool:
    if left.file_id != right.file_id:
        return False
    for named, worksheet in ((left, right), (right, left)):
        if (
            named.table_kind == "NAMED_TABLE"
            and worksheet.table_kind == "WORKSHEET"
            and named.worksheet_name == worksheet.table_name
        ):
            return True
    return False
