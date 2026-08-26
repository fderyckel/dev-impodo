"""Build and verify the deterministic Phase 0 Python import baseline."""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE_ROOT = ROOT / "src" / "impodo"
CURRENT_LOCATIONS = frozenset({"adapters", "application", "domain", "web"})


@dataclass(frozen=True)
class ProductionModule:
    """Describe one importable Python production module."""

    name: str
    path: Path
    is_package: bool
    location: str


@dataclass(frozen=True, order=True)
class ImportEdge:
    """Describe one internal import and whether it is type-checking-only."""

    importer: str
    imported: str
    type_only: bool


def module_location(relative_path: Path) -> str:
    """Classify the current mixed package shape without guessing capability."""

    parts = relative_path.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return "package_root"
    if parts[0] in CURRENT_LOCATIONS:
        return parts[0]
    if len(parts) == 1:
        return "legacy_root"
    return "unclassified"


def discover_modules(package_root: Path = DEFAULT_PACKAGE_ROOT) -> tuple[ProductionModule, ...]:
    """Return production modules in a stable path order."""

    modules: list[ProductionModule] = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root)
        parts = list(relative.with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts.pop()
        name = ".".join(("impodo", *parts))
        modules.append(
            ProductionModule(
                name=name,
                path=path,
                is_package=is_package,
                location=module_location(relative),
            )
        )
    return tuple(modules)


def _is_type_checking_guard(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == "TYPE_CHECKING"
        or isinstance(node, ast.Attribute)
        and node.attr == "TYPE_CHECKING"
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
    )


def _known_module(name: str, known_modules: frozenset[str]) -> str | None:
    candidate = name
    while candidate.startswith("impodo"):
        if candidate in known_modules:
            return candidate
        if "." not in candidate:
            break
        candidate = candidate.rsplit(".", 1)[0]
    return None


class _ImportVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        module: str,
        is_package: bool,
        known_modules: frozenset[str],
    ) -> None:
        self.module = module
        self.package = module if is_package else module.rpartition(".")[0]
        self.known_modules = known_modules
        self.type_only_depth = 0
        self.occurrences: list[tuple[str, bool]] = []

    def _record(self, candidate: str) -> None:
        target = _known_module(candidate, self.known_modules)
        if target is not None and target != self.module:
            self.occurrences.append((target, self.type_only_depth > 0))

    def visit_If(self, node: ast.If) -> None:  # noqa: N802 - ast API
        self.visit(node.test)
        guarded = _is_type_checking_guard(node.test)
        if guarded:
            self.type_only_depth += 1
        for child in node.body:
            self.visit(child)
        if guarded:
            self.type_only_depth -= 1
        for child in node.orelse:
            self.visit(child)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 - ast API
        for alias in node.names:
            self._record(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.level:
            relative_name = "." * node.level + (node.module or "")
            try:
                base = importlib.util.resolve_name(relative_name, self.package)
            except (ImportError, ValueError):
                return
        else:
            base = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                self._record(base)
                continue
            candidate = f"{base}.{alias.name}" if base else alias.name
            if _known_module(candidate, self.known_modules) is not None:
                self._record(candidate)
            else:
                self._record(base)


def imports_from_source(
    *,
    module: str,
    source: str,
    is_package: bool,
    known_modules: Iterable[str],
) -> tuple[ImportEdge, ...]:
    """Resolve internal imports, including relative and type-only imports."""

    known = frozenset(known_modules)
    visitor = _ImportVisitor(
        module=module,
        is_package=is_package,
        known_modules=known,
    )
    visitor.visit(ast.parse(source, filename=module))
    modes: dict[str, bool] = {}
    for imported, type_only in visitor.occurrences:
        modes[imported] = modes.get(imported, True) and type_only
    return tuple(
        sorted(
            ImportEdge(module, imported, type_only)
            for imported, type_only in modes.items()
        )
    )


def discover_imports(modules: Iterable[ProductionModule]) -> tuple[ImportEdge, ...]:
    """Read and resolve all internal imports in stable order."""

    materialized = tuple(modules)
    known = frozenset(module.name for module in materialized)
    edges: list[ImportEdge] = []
    for module in materialized:
        edges.extend(
            imports_from_source(
                module=module.name,
                source=module.path.read_text(encoding="utf-8"),
                is_package=module.is_package,
                known_modules=known,
            )
        )
    return tuple(sorted(edges))


def _strong_components(
    module_names: Iterable[str],
    edges: Iterable[ImportEdge],
) -> tuple[tuple[str, ...], ...]:
    adjacency = {name: [] for name in module_names}
    for edge in edges:
        adjacency[edge.importer].append(edge.imported)
    for targets in adjacency.values():
        targets.sort()

    index = 0
    indexes: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(name: str) -> None:
        nonlocal index
        indexes[name] = index
        low_links[name] = index
        index += 1
        stack.append(name)
        on_stack.add(name)
        for target in adjacency[name]:
            if target not in indexes:
                visit(target)
                low_links[name] = min(low_links[name], low_links[target])
            elif target in on_stack:
                low_links[name] = min(low_links[name], indexes[target])
        if low_links[name] != indexes[name]:
            return
        component: list[str] = []
        while True:
            target = stack.pop()
            on_stack.remove(target)
            component.append(target)
            if target == name:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for module_name in sorted(adjacency):
        if module_name not in indexes:
            visit(module_name)
    return tuple(sorted(components))


def _digest(value: object) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_snapshot(package_root: Path = DEFAULT_PACKAGE_ROOT) -> dict[str, object]:
    """Build the concise, reviewable Phase 0 architecture snapshot."""

    modules = discover_modules(package_root)
    edges = discover_imports(modules)
    module_locations = {module.name: module.location for module in modules}
    module_names = tuple(sorted(module_locations))
    serialized_edges = [
        [edge.importer, edge.imported, "type_only" if edge.type_only else "runtime"]
        for edge in edges
    ]
    location_counts = {
        location: sum(module.location == location for module in modules)
        for location in sorted(set(module_locations.values()))
    }
    runtime_edges = tuple(edge for edge in edges if not edge.type_only)
    return {
        "schema_version": 1,
        "module_count": len(modules),
        "module_digest": _digest(module_names),
        "modules_by_location": location_counts,
        "unclassified_modules": [
            module.name for module in modules if module.location == "unclassified"
        ],
        "runtime_import_edge_count": len(runtime_edges),
        "type_only_import_edge_count": len(edges) - len(runtime_edges),
        "import_edge_digest": _digest(serialized_edges),
        "runtime_cycles": [
            list(component)
            for component in _strong_components(module_names, runtime_edges)
        ],
        "cycles_including_type_only": [
            list(component) for component in _strong_components(module_names, edges)
        ],
        "application_to_adapter_edges": [
            [edge.importer, edge.imported, "type_only" if edge.type_only else "runtime"]
            for edge in edges
            if module_locations[edge.importer] == "application"
            and module_locations[edge.imported] == "adapters"
        ],
    }


def _formatted(snapshot: object) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=DEFAULT_PACKAGE_ROOT)
    parser.add_argument("--check", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    snapshot = build_snapshot(args.package_root)
    if args.check is not None:
        expected = json.loads(args.check.read_text(encoding="utf-8"))
        if snapshot != expected:
            print(
                "".join(
                    difflib.unified_diff(
                        _formatted(expected).splitlines(keepends=True),
                        _formatted(snapshot).splitlines(keepends=True),
                        fromfile=str(args.check),
                        tofile="current architecture inventory",
                    )
                ),
                end="",
            )
            return 1
        print(f"Architecture baseline matches {args.check}")
    if args.json or args.check is None:
        print(_formatted(snapshot), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
