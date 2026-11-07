"""Explain saved preparation checks without changing their evidence."""

from __future__ import annotations

import re

SOURCE_IDENTITY_REASONS = frozenset(
    {
        "SOURCE_IDENTITY_INVALID",
        "SOURCE_IDENTITY_DUPLICATE",
    }
)


def quality_reason_message(
    code: str,
    message: str,
    fields: tuple[str, ...],
    *,
    source_row_identifier: bool = True,
) -> str:
    """Name the identifier columns and distinguish source checks from Odoo."""

    columns = ", ".join(fields) if fields else "the selected source columns"
    if code == "SOURCE_IDENTITY_INVALID":
        problem = (
            "is empty"
            if message == "required value is empty"
            else f"cannot be used: {message.rstrip('.')}"
            if message
            else "is empty or invalid"
        )
        subject = (
            f"The source row identifier ({columns})"
            if source_row_identifier
            else f"The identifying value ({columns})"
        )
        consequence = (
            "Every included source row needs a complete identifier so Impodo "
            "can distinguish it from other source rows."
            if source_row_identifier
            else "Impodo needs a usable value to prepare the Odoo matching rule for this row."
        )
        return f"{subject} {problem}. {consequence}"
    if code == "SOURCE_IDENTITY_DUPLICATE":
        empty_group = re.fullmatch(
            r"source identity \((?:None,\s*)*None,?\) occurs ([0-9]+) times",
            message,
        )
        if empty_group is not None:
            return (
                f"{empty_group[1]} source rows have an empty row identifier "
                f"({columns}). They are grouped together because their "
                "identifiers are empty. This compares source rows with each "
                "other, not with Odoo records."
            )
        return (
            f"The source row identifier ({columns}) is shared by more than one "
            "source row. Empty identifiers can also be grouped together by this "
            "check. This compares source rows with each other, not with Odoo records."
        )
    return message


def source_identity_next_step(code: str, *, source_row_identifier: bool = True) -> str:
    """Describe the source correction separately from the later preparation."""

    if code == "SOURCE_IDENTITY_INVALID":
        if not source_row_identifier:
            return (
                "Review the affected source values and their Odoo matching rule "
                "in Match data. Complete or correct the identifying values, "
                "then prepare the complete data again."
            )
        return (
            "Review the affected source rows and complete or correct their "
            "identifier values. In Match data, check that Unique row identifier "
            "uses the intended column or combination of columns. Then prepare "
            "the complete data again."
        )
    return (
        "Review the affected source rows. Correct repeated identifier values, "
        "or choose a column or combination that uniquely identifies each row "
        "under Unique row identifier in Match data. Complete any empty "
        "identifiers first, then prepare the complete data again."
    )
