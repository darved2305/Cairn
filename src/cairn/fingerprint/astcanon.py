"""Canonical Python AST units used by Cairn's structural fingerprints."""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodeUnit:
    unit_id: str
    module: str
    qualname: str
    ast_digest: str
    docstring_digest: str
    is_private: bool
    source_path: str


def canonical_source(node: ast.AST) -> str:
    """Return normalized source with docstrings and location data excluded."""

    normalized = _DocstringStripper().visit(copy.deepcopy(node))
    assert normalized is not None
    ast.fix_missing_locations(normalized)
    return ast.unparse(normalized)


def canon_digest(path: str | Path) -> str:
    """Digest a Python file modulo comments, formatting, and docstrings."""

    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path), type_comments=True)
    return node_digest(tree)


def node_digest(node: ast.AST) -> str:
    return hashlib.sha256(canonical_source(node).encode("utf-8")).hexdigest()


def extract_units(path: str | Path, module: str) -> tuple[list[CodeUnit], dict[str, ast.AST]]:
    """Extract module, top-level symbol, and top-level binding units.

    A function or class is one unit including its nested definitions. Simple
    module assignments become units as well so changing a default such as
    ``HIDDEN_DIM`` changes the fingerprint of stages that read that global.
    """

    source_path = Path(path)
    tree = ast.parse(
        source_path.read_text(encoding="utf-8"), filename=str(path), type_comments=True
    )
    nodes: dict[str, ast.AST] = {}

    root_body: list[ast.stmt] = []
    for statement in tree.body:
        names = _bound_names(statement)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes[f"{module}:{statement.name}"] = statement
        elif names:
            for name in names:
                nodes[f"{module}:{name}"] = statement
        else:
            root_body.append(statement)
    nodes[f"{module}:<module>"] = ast.Module(body=root_body, type_ignores=tree.type_ignores)

    units = [
        CodeUnit(
            unit_id=unit_id,
            module=module,
            qualname=unit_id.split(":", 1)[1],
            ast_digest=node_digest(node),
            docstring_digest=_docstring_digest(node),
            is_private=_is_private(unit_id.split(":", 1)[1]),
            source_path=str(source_path),
        )
        for unit_id, node in sorted(nodes.items())
    ]
    return units, nodes


class _DocstringStripper(ast.NodeTransformer):
    def _strip(
        self, node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:]

    def visit_Module(self, node: ast.Module) -> ast.AST:
        self._strip(node)
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self._strip(node)
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self._strip(node)
        return self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self._strip(node)
        return self.generic_visit(node)


def _bound_names(statement: ast.stmt) -> list[str]:
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        return sorted({name for target in targets for name in _target_names(target)})
    return []


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for element in node.elts for name in _target_names(element)]
    return []


def _docstring_digest(node: ast.AST) -> str:
    value = (
        ast.get_docstring(node, clean=False)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        else None
    )
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _is_private(qualname: str) -> bool:
    leaf = qualname.rsplit(".", 1)[-1]
    return leaf.startswith("_") and not (leaf.startswith("__") and leaf.endswith("__"))
