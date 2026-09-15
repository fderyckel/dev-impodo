"""Compile governed authoring contracts without reading rows or running engines.

Browser mappings and profiles share ``CompiledMigrationPlan`` for downstream
target semantics. The columnar compiler separately describes supported browser
transformations and records whole-dataset fallback decisions for preparation.
"""

from .columnar_transformation import (
    COLUMNAR_CAPABILITY_MATRIX,
    ColumnarCompilationDecision,
    ColumnarDatasetKind,
    ColumnarSupport,
    ColumnarTransformationProgram,
    compile_columnar_transformation_program,
    compile_columnar_transformation_programs,
)
from .contracts import CompiledMigrationPlan
from .profile_compiler import compile_profile_document


__all__ = [
    "COLUMNAR_CAPABILITY_MATRIX",
    "ColumnarCompilationDecision",
    "ColumnarDatasetKind",
    "ColumnarSupport",
    "ColumnarTransformationProgram",
    "CompiledMigrationPlan",
    "compile_columnar_transformation_program",
    "compile_columnar_transformation_programs",
    "compile_profile_document",
]
