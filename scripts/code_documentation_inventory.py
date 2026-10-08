"""Report advisory Python docstring coverage for ``src/impodo``.

The report deliberately counts public symbols without enforcing a target.
Reviewers use it to find navigation gaps; documented exceptions such as
obvious accessors or passive data carriers may remain without docstrings.

``--check`` enforces the package-wide module-docstring floor and rejects the
retired A-K stage taxonomy in comments and docstrings. Public symbol results
remain advisory until the repository has a reviewed baseline of intentional
exceptions; this avoids rewarding repetitive or misleading docstrings merely
to satisfy a percentage.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
import io
from pathlib import Path
import re
import tokenize
from typing import Iterable, Sequence


LEGACY_STAGE_REFERENCE_RE = re.compile(
    r"\bStages?(?:\s+|-)[A-K](?:\s*(?:-|–|—|through|to)\s*[A-K])?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ModuleDocumentation:
    """Summarize docstring coverage for one parsed Python module."""

    path: Path
    line_count: int
    has_module_docstring: bool
    public_symbol_count: int
    documented_symbol_count: int
    missing_symbols: tuple[str, ...]

    @property
    def area(self) -> str:
        relative = self.path.parts
        return relative[0] if len(relative) > 1 else "(root)"


@dataclass(frozen=True, slots=True, order=True)
class LegacyStageReference:
    """Locate one retired A-K workflow label in developer-facing Python prose."""

    path: Path
    line: int
    text: str

    def render(self) -> str:
        """Return one concise review diagnostic."""

        return f"{self.path.as_posix()}:{self.line}: {self.text.strip()}"


def inspect_module(path: Path, *, package_root: Path) -> ModuleDocumentation:
    """Parse one module and inspect top-level APIs plus public class methods."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    symbols: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_"):
            symbols.append(node)
        if isinstance(node, ast.ClassDef):
            symbols.extend(
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not child.name.startswith("_")
            )

    missing = tuple(
        node.name for node in symbols if ast.get_docstring(node) is None
    )
    return ModuleDocumentation(
        path=path.relative_to(package_root),
        line_count=len(source.splitlines()),
        has_module_docstring=ast.get_docstring(tree) is not None,
        public_symbol_count=len(symbols),
        documented_symbol_count=len(symbols) - len(missing),
        missing_symbols=missing,
    )


def inspect_package(package_root: Path) -> tuple[ModuleDocumentation, ...]:
    """Return deterministic documentation records for every Python module."""

    return tuple(
        inspect_module(path, package_root=package_root)
        for path in sorted(package_root.rglob("*.py"))
    )


def legacy_stage_references(
    source_roots: Sequence[Path],
    *,
    repo_root: Path,
) -> tuple[LegacyStageReference, ...]:
    """Find retired A-K labels in Python comments and docstrings."""

    references: list[LegacyStageReference] = []
    for path in sorted(
        candidate
        for source_root in source_roots
        for candidate in source_root.rglob("*.py")
    ):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ) or not node.body:
                continue
            expression = node.body[0]
            if not (
                isinstance(expression, ast.Expr)
                and isinstance(expression.value, ast.Constant)
                and isinstance(expression.value.value, str)
            ):
                continue
            for offset, line in enumerate(expression.value.value.splitlines()):
                if LEGACY_STAGE_REFERENCE_RE.search(line):
                    references.append(
                        LegacyStageReference(
                            path=path.relative_to(repo_root),
                            line=expression.lineno + offset,
                            text=line,
                        )
                    )
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if (
                token.type == tokenize.COMMENT
                and LEGACY_STAGE_REFERENCE_RE.search(token.string)
            ):
                references.append(
                    LegacyStageReference(
                        path=path.relative_to(repo_root),
                        line=token.start[0],
                        text=token.string,
                    )
                )
    return tuple(sorted(set(references)))


def _summary_rows(
    modules: Iterable[ModuleDocumentation],
) -> list[tuple[str, int, int, int, int, int, int]]:
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for module in modules:
        values = totals[module.area]
        values[0] += 1
        values[1] += module.line_count
        values[2] += int(module.has_module_docstring)
        values[3] += module.public_symbol_count
        values[4] += module.documented_symbol_count
        values[5] += len(module.missing_symbols)
    return [
        (area, *values)
        for area, values in sorted(totals.items())
    ]


def render_summary(modules: Sequence[ModuleDocumentation]) -> str:
    """Render package-area totals as a Markdown table."""

    lines = [
        (
            "| Area | Modules | Lines | Module docs | Public symbols | "
            "Documented | Missing |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for area, count, lines_count, module_docs, public, documented, missing in (
        _summary_rows(modules)
    ):
        lines.append(
            f"| {area} | {count} | {lines_count} | {module_docs} | "
            f"{public} | {documented} | {missing} |"
        )
    lines.append(
        f"| **Total** | **{len(modules)}** | "
        f"**{sum(item.line_count for item in modules)}** | "
        f"**{sum(item.has_module_docstring for item in modules)}** | "
        f"**{sum(item.public_symbol_count for item in modules)}** | "
        f"**{sum(item.documented_symbol_count for item in modules)}** | "
        f"**{sum(len(item.missing_symbols) for item in modules)}** |"
    )
    return "\n".join(lines)


def render_missing(modules: Sequence[ModuleDocumentation]) -> str:
    """Render modules with undocumented public symbols, largest gaps first."""

    lines: list[str] = []
    ordered = sorted(
        (item for item in modules if item.missing_symbols),
        key=lambda item: (-len(item.missing_symbols), str(item.path)),
    )
    for module in ordered:
        names = ", ".join(module.missing_symbols)
        lines.append(f"{module.path}: {names}")
    return "\n".join(lines)


def undocumented_modules(
    modules: Sequence[ModuleDocumentation],
) -> tuple[Path, ...]:
    """Return modules missing their editor-orientation docstring."""

    return tuple(
        module.path for module in modules if not module.has_module_docstring
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the advisory inventory command."""

    parser = argparse.ArgumentParser(
        description="Report Python docstring coverage for the Impodo package."
    )
    parser.add_argument(
        "--package-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "src" / "impodo",
        help="package directory to inspect (default: repository src/impodo)",
    )
    parser.add_argument(
        "--missing",
        action="store_true",
        help="also list undocumented public symbols by module",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "fail when a module lacks a module docstring or Python prose uses "
            "the retired A-K taxonomy; public-symbol gaps remain advisory"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print the inventory and optionally enforce module-level orientation."""

    arguments = build_parser().parse_args(argv)
    package_root = arguments.package_root.resolve()
    modules = inspect_package(package_root)
    print(render_summary(modules))
    if arguments.missing:
        print()
        print(render_missing(modules))
    missing_modules = undocumented_modules(modules)
    retired_references = legacy_stage_references(
        (package_root,),
        repo_root=package_root,
    )
    if arguments.check:
        if missing_modules:
            print()
            print("Modules missing docstrings:")
            for path in missing_modules:
                print(path)
        if retired_references:
            print()
            print("Retired A-K stage references in Python prose:")
            for reference in retired_references:
                print(reference.render())
        if missing_modules or retired_references:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
