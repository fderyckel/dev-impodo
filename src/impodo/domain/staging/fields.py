"""Canonical synthetic field naming shared by compilation and evaluation."""


def synthetic_field(index: int) -> str:
    """Return the stable internal column used for a compiled scalar mapping."""

    return f"__impodo_scalar_{index}"


def synthetic_relationship_field(
    relationship_index: int,
    source_index: int,
) -> str:
    """Return one private input column for a compiled relationship mapping."""

    return f"__impodo_relationship_{relationship_index}_{source_index}"
