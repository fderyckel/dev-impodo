"""Report advisory Python docstring coverage for ``src/impodo``.

The report deliberately counts public symbols without enforcing a target.
Reviewers use it to find navigation gaps; documented exceptions such as
obvious accessors or passive data carriers may remain without docstrings.

``--check`` enforces only the package-wide module-docstring floor. Public
symbol results remain advisory until the repository has a reviewed baseline
of intentional exceptions; this avoids rewarding repetitive or misleading
docstrings merely to satisfy a percentage.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


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
            "fail only when a Python module lacks a module docstring; "
            "public-symbol gaps remain advisory"
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
    if arguments.check and missing_modules:
        print()
        print("Modules missing docstrings:")
        for path in missing_modules:
            print(path)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
