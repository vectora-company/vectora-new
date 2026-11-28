from __future__ import annotations

from pathlib import Path

from checks import main
from no_any import find_violations

SOURCE = """
from typing import Any, Literal

type Alias = dict[str, Any]
value: "list[object]" = []
kind: Literal["object"] = "object"


class Thing:
    def __eq__(self, other: object) -> bool:
        return False

    def read(self, raw: object, *rest: int) -> str:
        return str(raw)
"""


def test_reports_aliases_quoted_and_parameter_annotations(tmp_path: Path) -> None:
    module = tmp_path / "sample.py"
    module.write_text(SOURCE, encoding="utf-8")

    found = [item.annotation for item in find_violations(module)]

    assert found == ["dict[str, Any]", "'list[object]'", "object"]


def test_command_fails_only_when_something_is_found(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text("value: int = 1\n", encoding="utf-8")
    dirty = tmp_path / "dirty.py"
    dirty.write_text("value: object = 1\n", encoding="utf-8")

    assert main(["no-any", str(clean)]) == 0
    assert main(["no-any", str(dirty)]) == 1
