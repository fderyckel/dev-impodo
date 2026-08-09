"""Compilation of governed authoring contracts into runtime semantics."""

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
