"""Project safe-formula parser results into bounded browser authoring issues."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.recipe.value_rules import (
    FormulaValidationError,
    validate_formula,
)
from impodo.domain.workspace.contracts import SourceDataset, SourceSelection


MAPPING_FORMULA_ISSUE_CODE = "MAPPING_FORMULA_INVALID"


@dataclass(frozen=True, slots=True)
class FormulaAuthoringIssue:
    """One non-authoritative formula issue suitable for the mapping browser."""

    message: str
    correction: str
    position: int | None = None
    dataset_id: str | None = None
    dataset_index: int | None = None
    target_field: str | None = None
    path: str | None = None
    code: str = MAPPING_FORMULA_ISSUE_CODE
    severity: str = "error"

    @property
    def display_message(self) -> str:
        location = (
            f" Near character {self.position}."
            if self.position is not None
            else ""
        )
        return f"{self.message}{location} {self.correction}"

    def portable_dict(self) -> dict[str, object]:
        """Return only stable, formula-free fields for one browser response."""

        payload: dict[str, object] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "correction": self.correction,
            "position": self.position,
            "display_message": self.display_message,
        }
        for name, value in (
            ("dataset_id", self.dataset_id),
            ("dataset_index", self.dataset_index),
            ("target_field", self.target_field),
            ("path", self.path),
        ):
            if value is not None:
                payload[name] = value
        return payload


def formula_allowed_names(dataset: SourceDataset) -> set[str]:
    """Return the exact portable aliases available for one source dataset."""

    return {
        f"column_{getattr(column, 'ordinal', index + 1)}"
        for index, column in enumerate(dataset.columns)
    }


def validate_formula_authoring(
    expression: str,
    *,
    allowed_names: set[str],
) -> FormulaAuthoringIssue | None:
    """Return one authoring issue while leaving parser authority in domain."""

    if not expression.strip():
        return None
    try:
        validate_formula(expression, allowed_names=allowed_names)
    except FormulaValidationError as error:
        return FormulaAuthoringIssue(
            message=_authoring_message(error),
            correction=_authoring_correction(error),
            position=error.position,
        )
    except ValueError:
        return FormulaAuthoringIssue(
            message="This formula is not valid.",
            correction="Use only the values, operators, and helpers listed here.",
        )
    return None


def mapping_formula_authoring_issues(
    datasets: Iterable[DatasetMapping],
    selection: SourceSelection,
) -> tuple[FormulaAuthoringIssue, ...]:
    """Check only formulas in one draft without creating validation evidence."""

    source_by_id = {dataset.dataset_id: dataset for dataset in selection.datasets}
    issues: list[FormulaAuthoringIssue] = []
    for dataset_index, dataset in enumerate(datasets):
        source = source_by_id.get(dataset.dataset_id)
        if source is None:
            continue
        allowed_names = formula_allowed_names(source)
        for field in dataset.fields:
            issue = validate_formula_authoring(
                field.transform.formula,
                allowed_names=allowed_names,
            )
            if issue is None:
                continue
            issues.append(
                replace(
                    issue,
                    dataset_id=dataset.dataset_id,
                    dataset_index=dataset_index,
                    target_field=field.target_field,
                    path=(
                        f"/datasets/{dataset_index}/fields/"
                        f"{field.target_field}/transform/formula"
                    ),
                )
            )
    return tuple(issues)


def formula_issues_by_dataset(
    issues: Iterable[FormulaAuthoringIssue],
) -> dict[str, dict[str, dict[str, object]]]:
    """Index complete issue payloads for server-rendered field feedback."""

    indexed: dict[str, dict[str, dict[str, object]]] = {}
    for issue in issues:
        if issue.dataset_id is None or issue.target_field is None:
            continue
        indexed.setdefault(issue.dataset_id, {})[issue.target_field] = (
            issue.portable_dict()
        )
    return indexed


def saved_with_formula_issues_message(issue_count: int) -> str:
    """Describe a successful draft save that still needs formula correction."""

    noun = "formula" if issue_count == 1 else "formulas"
    return (
        "Saved — needs attention: Your progress was saved. "
        f"Correct {issue_count} {noun} before checking the matches."
    )


def _authoring_message(error: FormulaValidationError) -> str:
    if error.reason == "FORMULA_SYNTAX_INVALID":
        return "This formula is not valid."
    if error.reason == "FORMULA_TOO_LONG":
        return "This formula is longer than Impodo allows."
    if error.reason == "FORMULA_TOO_COMPLEX":
        return "This formula is too complex."
    if error.reason == "FORMULA_UNKNOWN_VALUE":
        return "This formula uses a value that is not available here."
    message = str(error).rstrip(".")
    return f"{message}."


def _authoring_correction(error: FormulaValidationError) -> str:
    if error.reason == "FORMULA_SYNTAX_INVALID":
        return (
            "Use == for equals or != for does not equal, and check quotes and "
            "brackets."
        )
    if error.reason == "FORMULA_TOO_LONG":
        return "Shorten the formula or split the preparation into governed steps."
    if error.reason == "FORMULA_TOO_COMPLEX":
        return "Simplify the calculation into fewer operations."
    if error.reason == "FORMULA_UNKNOWN_VALUE":
        return "Use value or one of the available column_N names shown below."
    if error.reason in {
        "FORMULA_FUNCTION_UNSUPPORTED",
        "FORMULA_NAMED_ARGUMENT_UNSUPPORTED",
    }:
        return "Use only the listed helper functions with positional values."
    return "Use only the values, operators, and helpers listed here."
