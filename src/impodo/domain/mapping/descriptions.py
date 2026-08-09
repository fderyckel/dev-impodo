"""Render stable, review-facing descriptions of authored mapping semantics."""

from __future__ import annotations

from .contracts import ScalarFieldMapping, ScalarValueSource


def transformation_rule_summary(field: ScalarFieldMapping) -> str:
    """Describe one scalar program using the established impact-row wording."""

    rules = []
    if field.value_source is ScalarValueSource.CONSTANT:
        rules.append("Constant")
    elif field.value_source is ScalarValueSource.SOURCE_WITH_FALLBACK:
        rules.append("Source + fallback")
    else:
        rules.append("Source")
    if field.value_mappings:
        rules.append(f"Match {len(field.value_mappings)} source choice(s)")
    if field.reference_lookup is not None:
        rules.append("Approved reference lookup")
    transform = field.transform
    if transform.formula:
        rules.append("Formula")
    if transform.trim:
        rules.append("Trim")
    if transform.collapse_whitespace:
        rules.append("Collapse spaces")
    if transform.search_value:
        rules.append("Find and replace")
    if transform.case_mode != "preserve":
        rules.append(f"Case: {transform.case_mode}")
    if transform.empty_as_null:
        rules.append("Empty to null")
    if field.value_type != "string":
        rules.append(f"Parse {field.value_type}")
    if transform.decimal_places is not None:
        rules.append(f"Round to {transform.decimal_places} places")
    if field.validation.configured:
        rules.append("Final value check")
    return " + ".join(rules)


__all__ = ["transformation_rule_summary"]
