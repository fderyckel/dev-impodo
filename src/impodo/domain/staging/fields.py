"""Canonical synthetic field naming shared by compilation and evaluation."""


def synthetic_field(index: int) -> str:
    return f"__impodo_scalar_{index}"
