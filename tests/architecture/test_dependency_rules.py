"""Enforce Phase 1 production dependency direction while modules remain flat."""

from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

import ast
import importlib.util
from pathlib import Path
import unittest

from scripts.architecture_inventory import (
    ImportEdge,
    ProductionModule,
    discover_imports,
    discover_modules,
)


ROOT = REPOSITORY_ROOT
FORBIDDEN_IMPORTS = {
    "domain": frozenset({"application", "adapters", "web"}),
    "application": frozenset({"adapters", "web"}),
}
ADAPTER_CONSTRUCTION_MODULES = frozenset(
    {
        "impodo.web.app",
        "impodo.web.capability_builders",
        "impodo.web.composition.cli",
        "impodo.web.composition.preparation_worker",
        "impodo.web.composition.incompatible_project_storage",
        "impodo.web.composition.target_readers",
        "impodo.web.composition.target_writers",
    }
)


def _edge_text(edge: ImportEdge) -> str:
    mode = "type-only" if edge.type_only else "runtime"
    return f"{edge.importer} -> {edge.imported} ({mode})"


def _adapter_construction_violations(
    modules: tuple[ProductionModule, ...],
) -> tuple[str, ...]:
    """Return direct imported-adapter class construction outside composition."""

    violations: list[str] = []
    for module in modules:
        if module.location == "adapters":
            continue
        tree = ast.parse(
            module.path.read_text(encoding="utf-8"), filename=str(module.path)
        )
        package = module.name if module.is_package else module.name.rpartition(".")[0]
        adapter_classes: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                try:
                    imported_module = importlib.util.resolve_name(
                        "." * node.level + (node.module or ""),
                        package,
                    )
                except (ImportError, ValueError):
                    continue
            else:
                imported_module = node.module or ""
            if not imported_module.startswith("impodo.adapters"):
                continue
            adapter_classes.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name[:1].isupper() and not alias.name.endswith("Error")
            )
        if module.name in ADAPTER_CONSTRUCTION_MODULES:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in adapter_classes
            ):
                violations.append(
                    f"{module.name}:{node.lineno} constructs {node.func.id}"
                )
    return tuple(sorted(violations))


class ArchitectureDependencyRuleTests(unittest.TestCase):
    def test_production_dependencies_point_inward(self) -> None:
        """Keep Phase 1 direction, ownership, construction, and cycle rules true."""

        modules = discover_modules()
        self.assertEqual(
            tuple(
                module.name
                for module in modules
                if module.location in {"legacy_root", "unclassified"}
            ),
            (),
            "Every production module must have an owner-qualified layer path.",
        )

        layers = {module.name: module.location for module in modules}
        violations = tuple(
            _edge_text(edge)
            for edge in discover_imports(modules)
            if layers[edge.importer] in FORBIDDEN_IMPORTS
            and layers[edge.imported] in FORBIDDEN_IMPORTS[layers[edge.importer]]
        )
        self.assertEqual(
            violations,
            (),
            "Forbidden layer import(s):\n" + "\n".join(violations),
        )

        runtime_edges = tuple(
            edge for edge in discover_imports(modules) if not edge.type_only
        )
        adjacency = {module.name: set() for module in modules}
        for edge in runtime_edges:
            adjacency[edge.importer].add(edge.imported)
        cycles = _strong_components(adjacency)
        self.assertEqual(
            cycles,
            (),
            "Runtime dependency cycle(s):\n"
            + "\n".join(" -> ".join(component) for component in cycles),
        )

        constructions = _adapter_construction_violations(modules)
        self.assertEqual(
            constructions,
            (),
            "Concrete adapter construction outside composition or a worker:\n"
            + "\n".join(constructions),
        )


def _strong_components(
    adjacency: dict[str, set[str]],
) -> tuple[tuple[str, ...], ...]:
    """Return stable non-trivial strongly connected components."""

    index = 0
    indexes: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        nonlocal index
        indexes[module] = index
        low_links[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)
        for imported in sorted(adjacency[module]):
            if imported not in indexes:
                visit(imported)
                low_links[module] = min(low_links[module], low_links[imported])
            elif imported in on_stack:
                low_links[module] = min(low_links[module], indexes[imported])
        if low_links[module] != indexes[module]:
            return
        component: list[str] = []
        while True:
            imported = stack.pop()
            on_stack.remove(imported)
            component.append(imported)
            if imported == module:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for module in sorted(adjacency):
        if module not in indexes:
            visit(module)
    return tuple(sorted(components))


if __name__ == "__main__":
    unittest.main()
