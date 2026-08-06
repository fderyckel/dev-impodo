"""Canonical synthetic field naming shared by compilation and evaluation."""


def synthetic_field(index: int) -> str:
    """Return the stable internal column used for a compiled scalar mapping."""

    return f"__impodo_scalar_{index}"
