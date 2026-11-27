"""Inventory Python mock and fixture occurrences, with file and line."""

import argparse
import ast
import json

# Usado só para ler a revisão do repositório de origem com git.
import subprocess  # nosec B404
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MOCK_NAMES = {
    "Mock",
    "MagicMock",
    "AsyncMock",
    "PropertyMock",
    "NonCallableMock",
    "NonCallableMagicMock",
    "create_autospec",
    "mock_open",
    "patch",
    "seal",
}
ASSERTIONS = {
    f"assert_{name}"
    for name in (
        "called",
        "called_once",
        "called_with",
        "called_once_with",
        "any_call",
        "has_calls",
        "not_called",
        "awaited",
        "awaited_once",
        "awaited_with",
        "awaited_once_with",
        "any_await",
        "has_awaits",
        "not_awaited",
    )
}
BUILTIN_FIXTURES = {
    "monkeypatch",
    "tmp_path",
    "tmp_path_factory",
    "capsys",
    "capfd",
    "caplog",
    "request",
    "recwarn",
}
FAKE_PREFIXES = (
    "Fake",
    "Stub",
    "Dummy",
    "Mock",
    "Spy",
    "_Fake",
    "_Stub",
    "_Dummy",
    "_Mock",
    "_Spy",
)


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def import_aliases(tree):
    """Local name -> canonical dotted name, for the mocking libraries only."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (
            "unittest",
            "unittest.mock",
        ):
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "unittest.mock" and alias.asname:
                    aliases[alias.asname] = alias.name
    return aliases


def classify_call(node, aliases):
    name = dotted(node.func)
    if name is None:
        # e.g. factory().assert_called_once()
        attr = getattr(node.func, "attr", None)
        return f"assert:{attr}" if attr in ASSERTIONS else None
    head, _, tail = name.partition(".")
    if head == "monkeypatch" and tail:
        return f"monkeypatch.{tail}"
    resolved = aliases[head] + (f".{tail}" if tail else "") if head in aliases else name
    if resolved.startswith(("unittest.mock.", "mock.")):
        short = resolved.removeprefix("unittest.").removeprefix("mock.")
        if short.split(".")[0] in MOCK_NAMES:
            return f"mock.{short}"
    if name == "SimpleNamespace" or name.endswith(".SimpleNamespace"):
        return "namespace.SimpleNamespace"
    last = name.rsplit(".", 1)[-1]
    return f"assert:{last}" if last in ASSERTIONS else None


def is_fixture(node):
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (dotted(target) or "").rsplit(".", 1)[-1] == "fixture":
            return True
    return False


def scan(source, relative):
    tree = ast.parse(source, relative)
    aliases = import_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if kind := classify_call(node, aliases):
                found.append({"kind": kind, "line": node.lineno})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                # Bare decorators are not Call nodes, so ast.walk would miss them.
                if not isinstance(decorator, ast.Call):
                    resolved = aliases.get(dotted(decorator) or "", "")
                    if resolved.startswith("unittest.mock."):
                        found.append(
                            {
                                "kind": f"mock.{resolved.rsplit('.', 1)[-1]}",
                                "line": decorator.lineno,
                            }
                        )
            if is_fixture(node):
                found.append(
                    {
                        "kind": "fixture:definition",
                        "name": node.name,
                        "line": node.lineno,
                    }
                )
            if node.name.startswith("test_") or is_fixture(node):
                for arg in (
                    node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                ):
                    if arg.arg in BUILTIN_FIXTURES:
                        found.append(
                            {"kind": f"fixture:builtin:{arg.arg}", "line": node.lineno}
                        )
        elif isinstance(node, ast.ClassDef) and node.name.startswith(FAKE_PREFIXES):
            found.append({"kind": "fake:class", "name": node.name, "line": node.lineno})
    return sorted(found, key=lambda item: (item["line"], item["kind"]))


def revision(path):
    # Comando fixo, sem shell; o caminho é argumento do próprio desenvolvedor.
    result = subprocess.run(  # nosec B603 B607
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return result.stdout.strip() or None


def inventory(source):
    totals = Counter()
    consumers = defaultdict(set)
    files = {}
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source).as_posix()
        found = scan(path.read_text(encoding="utf-8"), relative)
        if not found:
            continue
        files[relative] = found
        for item in found:
            totals[item["kind"]] += 1
            consumers[item["kind"]].add(relative)
    return {
        "schema_version": 1,
        "source_revision": revision(source),
        "scope": (
            "Static occurrences in every .py under the test tree. Indirect factories, "
            "names rebound at runtime and fixture consumers of local fixtures are not "
            "resolved, so this is a lower bound and not a coverage figure."
        ),
        "kinds": {
            kind: {"occurrences": count, "files": len(consumers[kind])}
            for kind, count in sorted(
                totals.items(), key=lambda pair: (-pair[1], pair[0])
            )
        },
        "files": files,
    }


def self_test():
    found = scan(
        "from unittest.mock import AsyncMock as AM, patch\n"
        "import pytest\n"
        "class FakeStore: pass\n"
        "@pytest.fixture\n"
        "def db(tmp_path): return AM()\n"
        "@patch\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(a, 'b', 1)\n"
        "    client.patch('/x')\n"
        "    probe.assert_not_awaited()\n",
        "sample.py",
    )
    kinds = Counter(item["kind"] for item in found)
    assert kinds == Counter(
        [
            "fake:class",
            "fixture:definition",
            "fixture:builtin:tmp_path",
            "mock.AsyncMock",
            "mock.patch",
            "fixture:builtin:monkeypatch",
            "monkeypatch.setattr",
            "assert:assert_not_awaited",
        ]
    ), kinds


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT.parent / "tests")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "maps/mock-inventory.json"
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("ok")
    else:
        result = inventory(args.source)
        args.output.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        for kind, value in list(result["kinds"].items())[:30]:
            print(f"{value['occurrences']:6} {value['files']:4}  {kind}")
        print(
            len(result["kinds"]),
            "kinds,",
            sum(v["occurrences"] for v in result["kinds"].values()),
            "occurrences",
        )
