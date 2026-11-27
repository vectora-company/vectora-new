"""Compile, then run, every generated test target and classify each case."""

import argparse
import json
import re
import shutil

# Roda só cargo e os binários de teste gerados, estes isolados pelo bwrap.
import subprocess  # nosec B404
import tempfile
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASE = re.compile(r"#\[test\]\s*fn\s+(?:r#)?(case_\w+)")
RESULT = re.compile(r"^test cases::(?:r#)?(case_\w+) \.\.\. (ok|FAILED)$", re.M)
SECTION = re.compile(
    r"^---- cases::(?:r#)?(case_\w+) stdout ----\n(.*?)(?=^---- |^failures:$|^successes:$)",
    re.M | re.S,
)
SKIPPED = "SKIPPED "

# Order matters: the first match names the reason of a failure.
REASONS = [
    ("tdd_pending", "TDD pending: capability"),
    ("migration_pending_api", "migration pending api:"),
    ("migration_pending", "migration pending:"),
    ("assertion", "AssertionError"),
    ("python_error", "uncaught "),
]


def cargo(batch, target_dir, *arguments, timeout=900):
    # Comando fixo, sem shell e sem entrada externa; cargo vem do PATH do dev.
    return subprocess.run(  # nosec B603 B607
        [
            "cargo",
            "+stable",
            *arguments,
            "--manifest-path",
            str(batch / "Cargo.toml"),
            "--target-dir",
            str(target_dir),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def binaries(batch, target_dir):
    built = cargo(
        batch,
        target_dir,
        "build",
        "--tests",
        "--keep-going",
        "--jobs",
        "4",
        "--message-format",
        "json",
        timeout=5400,
    )
    found = {}
    errors = []
    for line in built.stdout.splitlines():
        if not line.startswith("{"):
            continue
        message = json.loads(line)
        if message.get("executable") and message.get("target", {}).get("kind") == [
            "test"
        ]:
            found[message["target"]["name"]] = message["executable"]
        inner = message.get("message") or {}
        if (
            message.get("reason") == "compiler-message"
            and inner.get("level") == "error"
        ):
            spans = inner.get("spans") or [{}]
            errors.append(
                {
                    "target": message.get("target", {}).get("name"),
                    "code": (inner.get("code") or {}).get("code"),
                    "message": inner.get("message", "")[:300],
                    "line": spans[0].get("line_start"),
                }
            )
    built.errors = errors
    return found, built


def execute(binary, timeout=120):
    """Run a test binary without network, with only a scratch directory writable."""
    with tempfile.TemporaryDirectory() as scratch:
        command = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",  # nosec B108 - tmpfs vazio dentro do sandbox
            "--bind",
            scratch,
            scratch,
            "--unshare-all",
            "--die-with-parent",
            "--chdir",
            scratch,
            "--clearenv",
            "--setenv",
            "HOME",
            scratch,
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            binary,
            "--test-threads=1",
            "--show-output",
        ]
        # Binário recém-compilado, sem rede e só com o scratch gravável.
        return subprocess.run(  # nosec B603
            command, capture_output=True, text=True, errors="replace", timeout=timeout
        )


def reason(output):
    for name, marker in REASONS:
        if marker in output:
            return name
    return "other"


def check(batch, target_dir, run):
    if run and not shutil.which("bwrap"):
        raise SystemExit("--run needs bubblewrap (bwrap) to isolate generated code")
    manifest = tomllib.loads((batch / "Cargo.toml").read_text())
    built, build = binaries(batch, target_dir)
    files = []
    totals = Counter()
    for target in manifest["test"]:
        source = (batch / target["path"]).read_text()
        cases = CASE.findall(source)
        entry = {
            "file": target["path"],
            "cases": len(cases),
            "compiles": target["name"] in built,
        }
        totals["cases"] += len(cases)
        if entry["compiles"]:
            totals["compiling_cases"] += len(cases)
        if entry["compiles"] and run:
            try:
                ran = execute(built[target["name"]])
                results = dict(RESULT.findall(ran.stdout))
                sections = dict(SECTION.findall(ran.stdout))
                outcome = {}
                for case, status in results.items():
                    if status == "ok":
                        skipped = SKIPPED in sections.get(case, "")
                        outcome[case] = "skipped" if skipped else "passed"
                    else:
                        outcome[case] = reason(sections.get(case, ""))
                    totals[outcome[case]] += 1
                entry["results"] = outcome
                entry["messages"] = {
                    k: v.strip()[:400]
                    for k, v in sections.items()
                    if outcome.get(k) != "passed"
                }
            except subprocess.TimeoutExpired:
                entry["results"] = "timeout"
                totals["timeout_files"] += 1
        files.append(entry)
    return {
        "summary": {
            "files": len(files),
            "compiling_files": sum(f["compiles"] for f in files),
            **dict(sorted(totals.items())),
            "note": "passed does not establish parity with the Python test; tdd_pending is the expected red.",
        },
        "build_errors": build.errors,
        "files": files,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "batch", type=Path, nargs="?", default=ROOT.parent / "rust-backend"
    )
    parser.add_argument("--target-dir", type=Path, default=ROOT / "target/gen")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--run", action="store_true", help="execute targets that compile"
    )
    args = parser.parse_args()
    report = check(args.batch.resolve(), args.target_dir.resolve(), args.run)
    output = args.output or ROOT / "reports" / "cargo-check.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report["summary"], indent=2))
