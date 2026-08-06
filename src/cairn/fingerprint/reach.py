"""Conservative static call/import graph and stage reachability."""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from cairn.fingerprint.astcanon import CodeUnit, extract_units

_ESCAPE_NAMES = frozenset(
    {"__import__", "eval", "exec", "getattr", "globals", "setattr", "vars", "entry_points"}
)
_ESCAPE_MODULES = frozenset({"importlib", "pkg_resources"})


@dataclass(frozen=True, order=True)
class CodeEdge:
    src_unit: str
    dst_unit: str
    edge_kind: str


@dataclass(frozen=True)
class CodeGraph:
    units: dict[str, CodeUnit]
    edges: frozenset[CodeEdge]
    escapes: dict[str, frozenset[str]]


@dataclass(frozen=True)
class Reachability:
    units: frozenset[str]
    sound: bool
    escape_hatches: frozenset[str]


def build_graph(source_root: str | Path) -> CodeGraph:
    root = Path(source_root).resolve()
    units: dict[str, CodeUnit] = {}
    nodes: dict[str, ast.AST] = {}

    for path in sorted(root.rglob("*.py")):
        if any(
            part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts
        ):
            continue
        module = _module_name(root, path)
        extracted, extracted_nodes = extract_units(path, module)
        for unit in extracted:
            units[unit.unit_id] = unit
            nodes[unit.unit_id] = extracted_nodes[unit.unit_id]

    imports_by_module = {
        unit.module: _collect_import_bindings(unit.module, nodes[unit.unit_id])
        for unit in units.values()
        if unit.qualname == "<module>"
    }

    edges: set[CodeEdge] = set()
    escapes: dict[str, frozenset[str]] = {}
    for unit_id, node in nodes.items():
        module = units[unit_id].module
        analyzer = _UnitAnalyzer(module, units, imports_by_module.get(module, {}))
        analyzer.visit(node)
        edges.update(CodeEdge(unit_id, dst, kind) for dst, kind in analyzer.edges)
        module_root = f"{module}:<module>"
        if unit_id != module_root:
            # Importing a function/class necessarily executes its module body.
            # Tracking that edge keeps top-level registrations and other
            # import-time effects inside the conservative fingerprint.
            edges.add(CodeEdge(unit_id, module_root, "imports"))
        if analyzer.escapes:
            escapes[unit_id] = frozenset(analyzer.escapes)

    return CodeGraph(units=units, edges=frozenset(edges), escapes=escapes)


def reachable(graph: CodeGraph, entrypoint: str) -> Reachability:
    if entrypoint not in graph.units:
        raise KeyError(f"entrypoint {entrypoint!r} is not present in the code graph")
    outgoing: dict[str, set[str]] = {}
    for edge in graph.edges:
        outgoing.setdefault(edge.src_unit, set()).add(edge.dst_unit)

    seen: set[str] = set()
    queue = deque([entrypoint])
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(sorted(outgoing.get(current, ())))

    found_escapes = {value for unit in seen for value in graph.escapes.get(unit, ())}
    return Reachability(
        units=frozenset(seen),
        sound=not found_escapes,
        escape_hatches=frozenset(found_escapes),
    )


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts:
        raise ValueError(f"source root {root} cannot itself be a Python package file")
    return ".".join(parts)


class _UnitAnalyzer(ast.NodeVisitor):
    def __init__(
        self,
        module: str,
        units: dict[str, CodeUnit],
        imports: dict[str, tuple[str, str | None]],
    ) -> None:
        self.module = module
        self.units = units
        self.edges: set[tuple[str, str]] = set()
        self.escapes: set[str] = set()
        self._imports = imports

    def visit_Call(self, node: ast.Call) -> None:
        target = self._resolve_expr(node.func)
        if target:
            self.edges.add((target, "calls"))
        escape = _escape_name(node.func)
        if escape:
            self.escapes.add(escape)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            target = self._resolve_name(node.id)
            if target:
                self.edges.add((target, "reads_global"))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".", 1)[0] in _ESCAPE_MODULES:
                self.escapes.add(alias.name)
            target = self._module_root(alias.name)
            if target:
                self.edges.add((target, "imports"))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        imported_module = _absolute_import(self.module, node.module or "", node.level)
        if imported_module.split(".", 1)[0] in _ESCAPE_MODULES:
            self.escapes.add(imported_module)
        target = self._module_root(imported_module)
        if target:
            self.edges.add((target, "imports"))
        self.generic_visit(node)

    def _resolve_expr(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._resolve_name(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            binding = self._imports.get(node.value.id)
            if binding:
                imported_module, symbol = binding
                if symbol is None:
                    return self._symbol_or_module(imported_module, node.attr)
                submodule = f"{imported_module}.{symbol}"
                if self._module_root(submodule):
                    return self._symbol_or_module(submodule, node.attr)
        return None

    def _resolve_name(self, name: str) -> str | None:
        local = f"{self.module}:{name}"
        if local in self.units:
            return local
        binding = self._imports.get(name)
        if not binding:
            return None
        imported_module, symbol = binding
        if symbol is None:
            return self._module_root(imported_module)
        return self._symbol_or_module(imported_module, symbol)

    def _symbol_or_module(self, module: str, symbol: str) -> str | None:
        target = f"{module}:{symbol}"
        if target in self.units:
            return target
        submodule = self._module_root(f"{module}.{symbol}")
        if submodule:
            return submodule
        return self._module_root(module)

    def _module_root(self, module: str) -> str | None:
        target = f"{module}:<module>"
        return target if target in self.units else None


def _absolute_import(current_module: str, imported: str, level: int) -> str:
    if level == 0:
        return imported
    package = current_module.split(".")[:-1]
    keep = max(0, len(package) - (level - 1))
    prefix = package[:keep]
    return ".".join([*prefix, imported]) if imported else ".".join(prefix)


def _collect_import_bindings(module: str, node: ast.AST) -> dict[str, tuple[str, str | None]]:
    bindings: dict[str, tuple[str, str | None]] = {}
    if not isinstance(node, ast.Module):
        return bindings
    for statement in node.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                bindings[alias.asname or alias.name.split(".")[0]] = (alias.name, None)
        elif isinstance(statement, ast.ImportFrom):
            imported_module = _absolute_import(module, statement.module or "", statement.level)
            for alias in statement.names:
                if alias.name != "*":
                    bindings[alias.asname or alias.name] = (imported_module, alias.name)
    return bindings


def _escape_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and node.id in _ESCAPE_NAMES:
        return node.id
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id in _ESCAPE_MODULES:
            return f"{node.value.id}.{node.attr}"
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "builtins"
            and node.attr in _ESCAPE_NAMES
        ):
            return f"builtins.{node.attr}"
    return None
