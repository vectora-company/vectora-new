"""Localiza anotações com ``Any`` ou ``object`` em arquivos Python.

A única exceção é o parâmetro de ``__eq__`` e ``__ne__``, que o protocolo do
Python exige como ``object``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN = frozenset({"Any", "object"})
OBJECT_REQUIRED = frozenset({"__eq__", "__ne__"})


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    annotation: str


def _names(annotation: ast.expr) -> Iterator[str]:
    """Nomes citados numa anotação, inclusive dentro de anotações entre aspas."""
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                quoted = ast.parse(node.value, mode="eval")
            except SyntaxError:
                continue
            yield from _names(quoted.body)


def _is_literal(annotation: ast.expr) -> bool:
    """``Literal["object"]`` cita um valor, não o tipo."""
    if not isinstance(annotation, ast.Subscript):
        return False
    base = annotation.value
    return getattr(base, "id", getattr(base, "attr", "")) == "Literal"


def _forbidden(annotation: ast.expr) -> bool:
    if _is_literal(annotation):
        return False
    return any(name in FORBIDDEN for name in _names(annotation))


def _annotations(tree: ast.Module) -> Iterator[ast.expr]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            parameters = [
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                args.vararg,
                args.kwarg,
            ]
            for parameter in parameters:
                if parameter is None or parameter.annotation is None:
                    continue
                if node.name in OBJECT_REQUIRED and (
                    ast.unparse(parameter.annotation) == "object"
                ):
                    continue
                yield parameter.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation
        elif isinstance(node, ast.TypeAlias):
            yield node.value


def find_violations(path: Path) -> list[Violation]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        (
            Violation(path.as_posix(), annotation.lineno, ast.unparse(annotation))
            for annotation in _annotations(tree)
            if _forbidden(annotation)
        ),
        key=lambda item: item.line,
    )
