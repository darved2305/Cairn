"""Deterministic change classification — PROJECT.md §3.3.

Six classes, each an explicit, mechanical proof obligation discharged over
the structural fingerprints D4 already computes (astcanon digests, reach.py
reachability) plus, where the proof needs it, a fresh AST re-parse of the
specific changed units. No class is asserted safe: every function here
returns a :class:`ClassVerdict` and the caller recomputes whenever
``applies`` is False or ``refused`` is True. This module never authorizes
reuse by itself — see PROJECT.md §3.1: "The model may propose reuse.
Deterministic evidence must authorize it." These functions ARE that
deterministic evidence; `db/decisions.py` (D5) is what turns a passing
verdict plus a passing probe into an ``authorized_by='structural'`` row.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum

from cairn.fingerprint.astcanon import CodeUnit, canonical_source, extract_units, node_digest
from cairn.fingerprint.reach import CodeGraph, reachable

_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
)
_SAFE_NESTED_CALLS = frozenset({"str", "repr", "len", "format", "int", "float", "round"})
_SPAWN_MARKERS = frozenset(
    {"Thread", "Process", "Popen", "ThreadPoolExecutor", "ProcessPoolExecutor", "fork", "spawn"}
)
_DYNAMIC_DISPATCH_MARKERS = frozenset(
    {"getattr", "setattr", "globals", "vars", "eval", "exec", "importlib", "__import__"}
)
_PRIVATE_NAME_PREFIX = "_"


class ChangeClass(StrEnum):
    COMMENT_ONLY = "comment_only"
    FORMATTING_ONLY = "formatting_only"
    LOGGING_ONLY = "logging_only"
    PRIVATE_SYMBOL_RENAME = "private_symbol_rename"
    UNREACHABLE_CHANGE = "unreachable_change"
    DOWNSTREAM_ONLY_CONFIG = "downstream_only_config"


@dataclass(frozen=True)
class ClassVerdict:
    """Result of attempting to discharge one class's proof obligation.

    - ``applies=False`` — this diff is not shaped like this class at all
      (e.g. the AST digest changed, so it isn't comment/formatting-only).
    - ``applies=True, refused=False`` — the proof holds; structural
      authorization for this class is available.
    - ``applies=True, refused=True`` — the diff has the right shape but a
      documented non-guarantee condition was detected in the reachable
      set, so the class is withheld. ``reason`` names the escape hatch.
    """

    change_class: ChangeClass
    applies: bool
    refused: bool
    reason: str


def _reachable_nodes(units: Iterable[CodeUnit]) -> list[ast.AST]:
    """Re-parse the reachable set's files and pull back each unit's node.

    reach.py's CodeGraph discards the parsed ast.AST nodes once digests are
    computed (only CodeUnit metadata survives). This scans the reachable
    set as it stands in the *current* checkout — correct, because deciding
    whether a class's non-guarantee condition (dynamic-dispatch, `__doc__`
    access, thread spawning, ...) holds is a question about what will
    execute going forward, not about a historical revision.
    """

    by_path: dict[str, list[CodeUnit]] = defaultdict(list)
    for unit in units:
        by_path[unit.source_path].append(unit)
    nodes: list[ast.AST] = []
    for path, path_units in by_path.items():
        _, parsed = extract_units(path, path_units[0].module)
        nodes.extend(parsed[unit.unit_id] for unit in path_units)
    return nodes


# ---------------------------------------------------------------------------
# comment_only / formatting_only
# ---------------------------------------------------------------------------


def classify_comment_or_formatting(
    old_unit: CodeUnit, new_unit: CodeUnit, *, reachable_units: Iterable[CodeUnit]
) -> ClassVerdict:
    """PROJECT.md §3.3 comment_only / formatting_only.

    Both classes share one proof: AST-normalization equality
    (``ast_digest`` unchanged — comments and whitespace never reach the
    AST, so an unchanged digest already proves the compiled semantics,
    docstrings excluded, are identical). The two classes differ only in
    *which* non-guarantee applies, and that is determined by whether the
    docstring itself changed:

    - ``docstring_digest`` differs too -> ``comment_only`` (a docstring is
      documentation-as-comment; refuse if the reachable set reads
      ``__doc__``/``inspect.getdoc``/uses doctest).
    - ``docstring_digest`` unchanged -> ``formatting_only`` (the edit was
      inline comments or pure whitespace; refuse if the reachable set
      parses a ``.py`` file as text).
    """

    if old_unit.ast_digest != new_unit.ast_digest:
        return ClassVerdict(
            ChangeClass.COMMENT_ONLY, applies=False, refused=False, reason="ast_digest changed"
        )

    nodes = _reachable_nodes(reachable_units)
    if old_unit.docstring_digest != new_unit.docstring_digest:
        hatch = _scan(nodes, _DocAccessScanner())
        if hatch:
            return ClassVerdict(
                ChangeClass.COMMENT_ONLY,
                applies=True,
                refused=True,
                reason=f"reachable code reads docstrings via {hatch}",
            )
        return ClassVerdict(ChangeClass.COMMENT_ONLY, applies=True, refused=False, reason="ok")

    hatch = _scan(nodes, _TextReadScanner())
    if hatch:
        return ClassVerdict(
            ChangeClass.FORMATTING_ONLY,
            applies=True,
            refused=True,
            reason=f"reachable code parses a .py file as text via {hatch}",
        )
    return ClassVerdict(ChangeClass.FORMATTING_ONLY, applies=True, refused=False, reason="ok")


class _DocAccessScanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hit: str | None = None

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "__doc__":
            self.hit = "__doc__"
        elif node.attr in ("getdoc", "getsource"):
            self.hit = f"inspect.{node.attr}"
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "__doc__":
            self.hit = "__doc__"
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if any(alias.name == "doctest" for alias in node.names):
            self.hit = "doctest"
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "doctest":
            self.hit = "doctest"
        self.generic_visit(node)


class _TextReadScanner(ast.NodeVisitor):
    """Conservative: any string literal containing ``.py`` in the same
    call as ``open``/``read_text``/``read`` is treated as a hit. Conservative
    mode prefers an unnecessary recompute over a missed text-parse case."""

    def __init__(self) -> None:
        self.hit: str | None = None

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in ("open", "read_text", "read"):
            literals = [
                a.value
                for a in ast.walk(node)
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            if any(".py" in lit for lit in literals):
                self.hit = f"{name}(...)"
        self.generic_visit(node)


def _scan(nodes: Iterable[ast.AST], visitor: ast.NodeVisitor) -> str | None:
    for node in nodes:
        visitor.visit(node)
        hit = getattr(visitor, "hit", None)
        if hit:
            return str(hit)
    return None


# ---------------------------------------------------------------------------
# logging_only
# ---------------------------------------------------------------------------


def classify_logging_only(
    *,
    old_unit_source: str,
    new_unit_source: str,
    new_file_source: str,
    reachable_units: Iterable[CodeUnit],
) -> ClassVerdict:
    """PROJECT.md §3.3 logging_only.

    ``old_unit_source``/``new_unit_source`` are the source text of just the
    one changed unit (e.g. a function definition) at each revision — the
    old and new revisions of a file can never both be read from the same
    path at once, so unlike the reachable-set scan below, the changed
    unit's two versions must be handed in directly rather than re-read
    from disk. ``new_file_source`` is the *whole* new file, needed only to
    find the module-level ``logger = logging.getLogger(...)`` binding a
    receiver must resolve to.

    Proof: strip every statement that is a discarded call to that
    module-level ``logging.Logger`` from both the old and the new version
    of the unit; the pruned trees must be AST-identical. Stripping (rather
    than diffing line-by-line) proves the claim regardless of where in the
    unit the logging call sits or how many were added or removed, which is
    exactly the shape of the D5 exit example (a ``logger.debug(...)``
    added inside a nested training loop).
    """

    old_node = ast.parse(old_unit_source).body[0]
    new_node = ast.parse(new_unit_source).body[0]
    if node_digest(old_node) == node_digest(new_node):
        return ClassVerdict(
            ChangeClass.LOGGING_ONLY,
            applies=False,
            refused=False,
            reason="no change (ast_digest equal)",
        )

    logger_names = _find_logger_names(ast.parse(new_file_source))
    if not logger_names:
        return ClassVerdict(
            ChangeClass.LOGGING_ONLY,
            applies=False,
            refused=False,
            reason="no module-level logging.Logger binding found",
        )

    unsafe = _find_unsafe_logger_statement(old_node, logger_names) or _find_unsafe_logger_statement(
        new_node, logger_names
    )
    if unsafe:
        return ClassVerdict(
            ChangeClass.LOGGING_ONLY,
            applies=False,
            refused=False,
            reason=f"logging statement is not provably side-effect-free: {unsafe}",
        )

    pruned_old = canonical_source(_LoggerCallStripper(logger_names).visit(_deepcopy(old_node)))
    pruned_new = canonical_source(_LoggerCallStripper(logger_names).visit(_deepcopy(new_node)))
    if pruned_old != pruned_new:
        return ClassVerdict(
            ChangeClass.LOGGING_ONLY,
            applies=False,
            refused=False,
            reason="diff is not confined to logger call statements",
        )

    reachable_nodes = _reachable_nodes(reachable_units)
    hatch = _scan(reachable_nodes, _SpawnScanner())
    if hatch:
        return ClassVerdict(
            ChangeClass.LOGGING_ONLY,
            applies=True,
            refused=True,
            reason=f"reachable set spawns threads/processes via {hatch}",
        )
    return ClassVerdict(ChangeClass.LOGGING_ONLY, applies=True, refused=False, reason="ok")


def _deepcopy(node: ast.AST) -> ast.AST:
    import copy

    return copy.deepcopy(node)


def _find_logger_names(module_node: ast.AST) -> frozenset[str]:
    names: set[str] = set()
    if not isinstance(module_node, ast.Module):
        return frozenset(names)
    for statement in module_node.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        value = statement.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        func = value.func
        is_get_logger = (isinstance(func, ast.Attribute) and func.attr == "getLogger") or (
            isinstance(func, ast.Name) and func.id == "getLogger"
        )
        if is_get_logger:
            names.add(target.id)
    return frozenset(names)


def _is_logger_call(node: ast.expr, logger_names: frozenset[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in logger_names
        and node.func.attr in _LOG_METHODS
    )


def _call_args_are_safe(call: ast.Call) -> bool:
    for arg_node in ast.walk(ast.Tuple(elts=list(call.args) + [kw.value for kw in call.keywords])):
        if isinstance(arg_node, ast.NamedExpr):
            return False
        if isinstance(arg_node, ast.Call):
            name = (
                arg_node.func.id
                if isinstance(arg_node.func, ast.Name)
                else getattr(arg_node.func, "attr", None)
            )
            if name not in _SAFE_NESTED_CALLS:
                return False
    return True


def _find_unsafe_logger_statement(node: ast.AST, logger_names: frozenset[str]) -> str | None:
    for statement in ast.walk(node):
        if not isinstance(statement, ast.Expr):
            continue
        call = statement.value
        if _is_logger_call(call, logger_names) and not _call_args_are_safe(call):  # type: ignore[arg-type]
            return ast.unparse(statement).strip()
    return None


class _LoggerCallStripper(ast.NodeTransformer):
    def __init__(self, logger_names: frozenset[str]) -> None:
        self.logger_names = logger_names

    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:
        if _is_logger_call(node.value, self.logger_names):
            return None
        return self.generic_visit(node)


class _SpawnScanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hit: str | None = None

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _SPAWN_MARKERS:
            self.hit = node.attr
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _SPAWN_MARKERS:
            self.hit = node.id
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# private_symbol_rename
# ---------------------------------------------------------------------------


def classify_private_symbol_rename(
    old_name: str,
    new_name: str,
    *,
    changed_units: Mapping[str, tuple[str, str]],
    reachable_units: Iterable[CodeUnit],
) -> ClassVerdict:
    """PROJECT.md §3.3 private_symbol_rename.

    ``changed_units`` maps an arbitrary label (typically a unit_id) to
    ``(old_source, new_source)`` — the source text of that one unit at
    each revision. There is one entry per unit touched by the rename: the
    definition itself plus every unit whose body calls it. Old and new
    revisions of the same file can never both be read from the same disk
    path at once, so the caller (which has both, e.g. from ``git show`` and
    the working tree, or two ``code_units`` snapshots) hands the text in
    directly rather than this function re-deriving it from a path.

    Proof: alpha-rename ``new_name`` back to ``old_name`` in each unit's
    new source; the result must be byte-identical to that unit's old
    canonical source. The hard refusal on dynamic dispatch is checked
    first because it defeats the proof entirely — a renamed symbol could
    still be reached through ``getattr(obj, name)`` with ``name`` held in
    a variable, which alpha-renaming cannot see.
    """

    if not old_name.startswith(_PRIVATE_NAME_PREFIX) or old_name.startswith("__"):
        return ClassVerdict(
            ChangeClass.PRIVATE_SYMBOL_RENAME,
            applies=False,
            refused=False,
            reason=f"{old_name!r} is not a private symbol (must match _[a-z]...)",
        )

    reachable_nodes = _reachable_nodes(reachable_units)
    hatch = _scan(reachable_nodes, _DynamicDispatchScanner())
    if hatch:
        return ClassVerdict(
            ChangeClass.PRIVATE_SYMBOL_RENAME,
            applies=True,
            refused=True,
            reason=f"reachable set uses dynamic dispatch via {hatch}",
        )

    for label, (old_source, new_source) in changed_units.items():
        old_node = ast.parse(old_source).body[0]
        new_node = ast.parse(new_source).body[0]
        old_canon = canonical_source(old_node)
        renamed_back = canonical_source(_RenameTransformer(new_name, old_name).visit(new_node))
        if renamed_back != old_canon:
            return ClassVerdict(
                ChangeClass.PRIVATE_SYMBOL_RENAME,
                applies=False,
                refused=False,
                reason=f"{label} differs by more than the rename after alpha-renaming back",
            )

    return ClassVerdict(ChangeClass.PRIVATE_SYMBOL_RENAME, applies=True, refused=False, reason="ok")


class _RenameTransformer(ast.NodeTransformer):
    def __init__(self, old: str, new: str) -> None:
        self.old = old
        self.new = new

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == self.old:
            node.id = self.new
        return self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if node.attr == self.old:
            node.attr = self.new
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name == self.old:
            node.name = self.new
        return self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        if node.arg == self.old:
            node.arg = self.new
        return self.generic_visit(node)


class _DynamicDispatchScanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hit: str | None = None

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _DYNAMIC_DISPATCH_MARKERS:
            self.hit = str(name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if any(alias.name == "importlib" for alias in node.names):
            self.hit = "importlib"
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "importlib":
            self.hit = "importlib"
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# unreachable_change
# ---------------------------------------------------------------------------


def classify_unreachable_change(
    old_graph: CodeGraph, new_graph: CodeGraph, entrypoint: str, changed_unit_ids: AbstractSet[str]
) -> ClassVerdict:
    """PROJECT.md §3.3 unreachable_change.

    Reachability is computed over the *union* of the old and new graphs so
    that a symbol which used to be reachable and no longer is (or vice
    versa) is still treated as potentially reachable — the class only
    applies when the changed unit was reachable in neither version.
    """

    old_reach = reachable(old_graph, entrypoint) if entrypoint in old_graph.units else None
    new_reach = reachable(new_graph, entrypoint) if entrypoint in new_graph.units else None
    if new_reach is None:
        return ClassVerdict(
            ChangeClass.UNREACHABLE_CHANGE,
            applies=False,
            refused=False,
            reason=f"entrypoint {entrypoint!r} not present in the new graph",
        )

    sound = new_reach.sound and (old_reach is None or old_reach.sound)
    union_reachable = new_reach.units | (old_reach.units if old_reach else frozenset())

    if not sound:
        escapes = new_reach.escape_hatches | (
            old_reach.escape_hatches if old_reach else frozenset()
        )
        return ClassVerdict(
            ChangeClass.UNREACHABLE_CHANGE,
            applies=True,
            refused=True,
            reason=f"reachability is unsound: {', '.join(sorted(escapes))}",
        )

    if changed_unit_ids & union_reachable:
        touched = sorted(changed_unit_ids & union_reachable)
        return ClassVerdict(
            ChangeClass.UNREACHABLE_CHANGE,
            applies=False,
            refused=False,
            reason=f"changed unit(s) reachable from entrypoint: {', '.join(touched)}",
        )

    return ClassVerdict(ChangeClass.UNREACHABLE_CHANGE, applies=True, refused=False, reason="ok")


# ---------------------------------------------------------------------------
# downstream_only_config
# ---------------------------------------------------------------------------


def classify_downstream_only_config(
    changed_config_keys: AbstractSet[str], recorded_config_reads: AbstractSet[str]
) -> ClassVerdict:
    """PROJECT.md §3.3 downstream_only_config.

    ``recorded_config_reads`` must come from the previous run's
    ``artifact_inputs`` rows with ``input_kind='config'`` — i.e. from
    :mod:`cairn.config`'s instrumented accessor, not from hashing the whole
    config file. That provenance requirement is the caller's job to
    satisfy (planner.py's ``artifact_inputs()`` already emits exactly
    these rows); this function only checks the set-membership condition.
    """

    if not changed_config_keys:
        return ClassVerdict(
            ChangeClass.DOWNSTREAM_ONLY_CONFIG,
            applies=False,
            refused=False,
            reason="no config keys changed",
        )
    if changed_config_keys & recorded_config_reads:
        overlapping = sorted(changed_config_keys & recorded_config_reads)
        return ClassVerdict(
            ChangeClass.DOWNSTREAM_ONLY_CONFIG,
            applies=False,
            refused=False,
            reason=f"artifact's recorded reads include changed key(s): {', '.join(overlapping)}",
        )
    return ClassVerdict(
        ChangeClass.DOWNSTREAM_ONLY_CONFIG, applies=True, refused=False, reason="ok"
    )
