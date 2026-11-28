"""Sugestão de mensagem de commit: lê o diff real e respeita os arquivos escolhidos."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services import commit_message as cm


def _git(repo: Path, *args: str) -> None:
    import git

    git.Repo(repo).git.execute(["git", *args])


@pytest.fixture
def repo(tmp_path: Path):
    import git

    git.Repo.init(tmp_path, initial_branch="main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def a():\n    return 1\n")
    (tmp_path / "README.md").write_text("# x\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "feat: primeira versão")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: ajusta algo")
    return git.Repo(tmp_path)


class TestCollect:
    def test_reads_the_real_diff_of_changed_files(self, repo) -> None:
        (Path(repo.working_tree_dir) / "src" / "app.py").write_text(
            "def a():\n    return 2\n"
        )

        ctx = cm.collect_change_context(repo)

        [changed] = ctx.files
        assert changed.path == "src/app.py"
        assert changed.status == "M"
        assert (changed.additions, changed.deletions) == (1, 1)
        assert "+    return 2" in changed.diff

    def test_only_considers_the_chosen_files(self, repo) -> None:
        root = Path(repo.working_tree_dir)
        (root / "src" / "app.py").write_text("def a():\n    return 2\n")
        (root / "README.md").write_text("# y\n")
        (root / "novo.py").write_text("x = 1\n")

        ctx = cm.collect_change_context(repo, ["README.md"])

        assert [f.path for f in ctx.files] == ["README.md"]

    def test_includes_untracked_files_with_a_preview(self, repo) -> None:
        (Path(repo.working_tree_dir) / "novo.py").write_text("x = 1\ny = 2\n")

        ctx = cm.collect_change_context(repo)

        [new] = ctx.files
        assert (new.path, new.status, new.additions) == ("novo.py", "?", 2)
        assert "x = 1" in new.diff

    def test_staged_and_unstaged_appear_together(self, repo) -> None:
        root = Path(repo.working_tree_dir)
        (root / "src" / "app.py").write_text("def a():\n    return 2\n")
        _git(root, "add", "src/app.py")
        (root / "README.md").write_text("# y\n")

        ctx = cm.collect_change_context(repo)

        assert {f.path for f in ctx.files} == {"src/app.py", "README.md"}

    def test_keeps_recent_commit_subjects(self, repo) -> None:
        ctx = cm.collect_change_context(repo)

        assert ctx.recent_subjects == ["fix: ajusta algo", "feat: primeira versão"]
        assert ctx.uses_conventional_commits

    @pytest.mark.parametrize("bad", ["/etc/passwd", "../fora", "a/../../b", "", "  "])
    def test_ignores_absolute_or_escaping_paths(self, bad: str) -> None:
        assert cm._clean_paths([bad]) == []

    def test_limits_the_number_of_paths(self) -> None:
        assert len(cm._clean_paths([f"f{i}.py" for i in range(1000)])) == 500

    def test_works_without_any_commit(self, tmp_path: Path) -> None:
        import git

        git.Repo.init(tmp_path, initial_branch="main")
        (tmp_path / "a.py").write_text("x = 1\n")
        _git(tmp_path, "add", "a.py")

        ctx = cm.collect_change_context(git.Repo(tmp_path))

        assert [f.path for f in ctx.files] == ["a.py"]


def _file(path: str, status: str = "M", add: int = 3, rem: int = 1):
    return cm.ChangedFile(path=path, status=status, additions=add, deletions=rem)


class TestHeuristic:
    def test_does_not_return_the_old_generic_text(self) -> None:
        ctx = cm.ChangeContext(files=[_file("src/app.py", "A")])

        suggestion = cm.heuristic_suggestion(ctx)

        assert "Atualiza 1 arquivo" not in suggestion.title
        assert "agrupadas" not in suggestion.description
        assert "app.py" in suggestion.title

    def test_new_file_becomes_feat(self) -> None:
        ctx = cm.ChangeContext(files=[_file("src/novo.py", "A", 20, 0)])

        assert cm.heuristic_suggestion(ctx).title.startswith("feat")

    def test_docs_only_becomes_docs(self) -> None:
        ctx = cm.ChangeContext(files=[_file("README.md"), _file("docs/a.md")])

        assert cm.heuristic_suggestion(ctx).title.startswith("docs")

    def test_tests_only_becomes_test(self) -> None:
        ctx = cm.ChangeContext(files=[_file("tests/test_a.py")])

        assert cm.heuristic_suggestion(ctx).title.startswith("test")

    def test_workflows_only_becomes_ci(self) -> None:
        ctx = cm.ChangeContext(files=[_file(".github/workflows/ci.yml")])

        assert cm.heuristic_suggestion(ctx).title.startswith("ci")

    def test_heavy_removal_becomes_refactor(self) -> None:
        ctx = cm.ChangeContext(files=[_file("src/a.py", "M", 5, 60)])

        assert cm.heuristic_suggestion(ctx).title.startswith("refactor")

    def test_scope_comes_from_the_common_directory(self) -> None:
        ctx = cm.ChangeContext(
            files=[
                _file("frontend/components/workbench/a.tsx"),
                _file("frontend/components/workbench/b.tsx"),
            ]
        )

        assert "(workbench)" in cm.heuristic_suggestion(ctx).title

    def test_no_scope_when_directories_are_generic(self) -> None:
        ctx = cm.ChangeContext(files=[_file("src/a.py"), _file("tests/b.py")])

        assert "(" not in cm.heuristic_suggestion(ctx).title.split(":")[0]

    def test_without_conventional_commits_title_is_a_plain_sentence(self) -> None:
        ctx = cm.ChangeContext(
            files=[_file("src/a.py")],
            recent_subjects=["Corrige bug", "Adiciona tela", "Remove código"],
        )

        title = cm.heuristic_suggestion(ctx).title

        assert not title.startswith(("feat", "fix"))
        assert title[0].isupper()

    def test_title_has_at_most_72_characters(self) -> None:
        ctx = cm.ChangeContext(
            files=[_file(f"src/modulo_com_nome_muito_longo_{i}.py") for i in range(5)]
        )

        assert len(cm.heuristic_suggestion(ctx).title) <= 72

    def test_description_lists_files_with_counts_and_summarizes_the_rest(
        self,
    ) -> None:
        ctx = cm.ChangeContext(files=[_file(f"src/f{i}.py") for i in range(11)])

        description = cm.heuristic_suggestion(ctx).description

        assert "- src/f0.py (alterado, +3 −1)" in description
        assert "e mais 3 arquivos" in description

    def test_no_files_suggests_nothing(self) -> None:
        suggestion = cm.heuristic_suggestion(cm.ChangeContext())

        assert (suggestion.title, suggestion.description) == ("", "")


class TestParseAnswer:
    def test_plain_json(self) -> None:
        parsed = cm._parse_model_answer('{"title": "feat: x", "description": "d"}')

        assert parsed == cm.CommitSuggestion(title="feat: x", description="d")

    def test_tolerates_code_fences_and_surrounding_text(self) -> None:
        raw = 'Claro!\n```json\n{"title": "fix: y.", "description": ""}\n```'

        parsed = cm._parse_model_answer(raw)

        assert parsed is not None
        assert parsed.title == "fix: y"

    @pytest.mark.parametrize(
        "raw", ["", "sem json", "[1]", '{"title": ""}', '{"x": 1}']
    )
    def test_rejects_invalid_answers(self, raw: str) -> None:
        assert cm._parse_model_answer(raw) is None

    def test_truncates_long_title(self) -> None:
        parsed = cm._parse_model_answer('{"title": "' + "a" * 200 + '"}')

        assert parsed is not None
        assert len(parsed.title) <= 72

    def test_uses_only_the_first_title_line(self) -> None:
        parsed = cm._parse_model_answer('{"title": "linha 1\\nlinha 2"}')

        assert parsed is not None
        assert parsed.title == "linha 1"


class _FakeLLM:
    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.messages: list = []

    async def agenerate(self, messages):
        self.messages = messages
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(text=lambda: self.answer)


@pytest.mark.asyncio
class TestGenerate:
    def _ctx(self):
        return cm.ChangeContext(
            files=[
                cm.ChangedFile(
                    "src/app.py", "M", 1, 1, "diff --git a/src/app.py b/src/app.py\n+x"
                )
            ],
            recent_subjects=["feat: a", "fix: b"],
        )

    async def test_uses_the_model_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _FakeLLM('{"title": "fix(app): corrige retorno", "description": "d"}')
        monkeypatch.setattr("backend.services.utils.load_native_llm", lambda *_: llm)

        suggestion = await cm.generate_commit_suggestion(self._ctx())

        assert suggestion.title == "fix(app): corrige retorno"

    async def test_prompt_carries_the_diff_and_recent_style(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _FakeLLM('{"title": "x"}')
        monkeypatch.setattr("backend.services.utils.load_native_llm", lambda *_: llm)

        await cm.generate_commit_suggestion(self._ctx())

        prompt = str(llm.messages[1])
        assert "+x" in prompt
        assert "src/app.py" in prompt
        assert "feat: a" in prompt
        assert "Conventional Commits" in prompt

    async def test_unavailable_model_falls_back_to_the_diff_heuristic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom[**P](*_args: P.args, **_kwargs: P.kwargs) -> None:
            raise RuntimeError("sem provider")

        monkeypatch.setattr("backend.services.utils.load_native_llm", boom)

        suggestion = await cm.generate_commit_suggestion(self._ctx())

        assert "app.py" in suggestion.title
        assert "Atualiza 1 arquivo" not in suggestion.title

    async def test_invalid_answer_falls_back_to_the_heuristic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "backend.services.utils.load_native_llm", lambda *_: _FakeLLM("lixo")
        )

        suggestion = await cm.generate_commit_suggestion(self._ctx())

        assert "app.py" in suggestion.title

    async def test_model_error_falls_back_to_the_heuristic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "backend.services.utils.load_native_llm",
            lambda *_: _FakeLLM(TimeoutError("lento")),
        )

        suggestion = await cm.generate_commit_suggestion(self._ctx())

        assert suggestion.title

    async def test_no_changes_does_not_call_the_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = []
        monkeypatch.setattr(
            "backend.services.utils.load_native_llm",
            lambda *_: called.append(1) or _FakeLLM("{}"),
        )

        suggestion = await cm.generate_commit_suggestion(cm.ChangeContext())

        assert (suggestion.title, called) == ("", [])

    async def test_diff_sent_to_the_model_is_limited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _FakeLLM('{"title": "x"}')
        monkeypatch.setattr("backend.services.utils.load_native_llm", lambda *_: llm)
        big = cm.ChangeContext(
            files=[cm.ChangedFile(f"f{i}.py", "M", 1, 0, "x" * 4000) for i in range(30)]
        )

        await cm.generate_commit_suggestion(big)

        assert len(str(llm.messages[1])) < 40_000
