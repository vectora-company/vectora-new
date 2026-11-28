"""Pessoas do repositório para co-autoria: lista, mescla, filtra e acha por usuário."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

from backend.services import git_people as gp
from backend.vtypes.contracts import JsonObject

SHORTLOG = (
    "    42\tAna Souza <ana@example.com>\n"
    "     7\tBruno Lima <12345+brunolima@users.noreply.github.com>\n"
    "     3\tdependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>\n"
    "     1\tGitHub <noreply@github.com>\n"
    "lixo sem formato\n"
)


class TestParseShortlog:
    def test_reads_name_email_and_count(self) -> None:
        people = gp.parse_shortlog(SHORTLOG)

        assert [(p.name, p.email, p.commits) for p in people] == [
            ("Ana Souza", "ana@example.com", 42),
            ("Bruno Lima", "12345+brunolima@users.noreply.github.com", 7),
        ]

    def test_skips_bots_and_malformed_lines(self) -> None:
        names = [p.name for p in gp.parse_shortlog(SHORTLOG)]

        assert "dependabot[bot]" not in names
        assert "GitHub" not in names

    def test_extracts_the_github_username_from_noreply_emails(self) -> None:
        by_name = {p.name: p for p in gp.parse_shortlog(SHORTLOG)}

        assert by_name["Bruno Lima"].username == "brunolima"
        assert by_name["Ana Souza"].username is None


class TestGithub:
    def test_contributors_become_people_with_a_noreply_email(self) -> None:
        raw = json.dumps(
            [
                {"login": "carla", "id": 99, "type": "User"},
                {"login": "robo", "id": 1, "type": "Bot"},
                {"nada": True},
            ]
        )

        [person] = gp.parse_gh_contributors(raw)

        assert person.username == "carla"
        assert person.email == "99+carla@users.noreply.github.com"
        assert person.source == "github"

    @pytest.mark.parametrize("raw", ["", "nope", "{}", "[1, 2]"])
    def test_invalid_contributor_payloads_give_nothing(self, raw: str) -> None:
        assert gp.parse_gh_contributors(raw) == []

    def test_user_lookup_uses_the_real_name_when_present(self) -> None:
        person = gp.parse_gh_user(
            json.dumps({"login": "dani", "id": 5, "name": "Dani X"})
        )

        assert person is not None
        assert (person.name, person.username) == ("Dani X", "dani")

    def test_user_lookup_without_name_falls_back_to_the_login(self) -> None:
        person = gp.parse_gh_user(json.dumps({"login": "dani", "id": 5, "name": None}))

        assert person is not None
        assert person.name == "dani"

    @pytest.mark.parametrize(
        "raw", ["", "x", json.dumps({"type": "Bot", "login": "b", "id": 1}), "{}"]
    )
    def test_user_lookup_rejects_bots_and_garbage(self, raw: str) -> None:
        assert gp.parse_gh_user(raw) is None


class TestUsername:
    @pytest.mark.parametrize("name", ["ana", "ana-souza", "A1", "a" * 39])
    def test_valid(self, name: str) -> None:
        assert gp.valid_username(name)

    @pytest.mark.parametrize(
        "name", ["", "-ana", "ana-", "a b", "ana/../x", "a" * 40, "ana;rm", "ana?x=1"]
    )
    def test_invalid_names_never_reach_the_gh_command(self, name: str) -> None:
        assert not gp.valid_username(name)


class TestMerge:
    def test_same_email_appears_once(self) -> None:
        a = gp.GitPerson(name="Ana", email="ana@x.com", commits=5)
        b = gp.GitPerson(name="Ana", email="ANA@x.com", commits=2)

        assert len(gp.merge_people([a], [b])) == 1

    def test_commit_noreply_and_github_collaborator_are_the_same_person(self) -> None:
        committed = gp.parse_shortlog("5\tBruno <1+bruno@users.noreply.github.com>")
        collaborator = [gp.github_person("bruno", 1)]

        merged = gp.merge_people(committed, collaborator)

        assert len(merged) == 1
        assert merged[0].commits == 5

    def test_committers_first_most_active_first_then_github_only(self) -> None:
        committers = [
            gp.GitPerson(name="Pouco", email="p@x.com", commits=1),
            gp.GitPerson(name="Muito", email="m@x.com", commits=9),
        ]
        collaborators = [gp.github_person("so-github", 7)]

        merged = gp.merge_people(committers, collaborators)

        assert [p.name for p in merged] == ["Muito", "Pouco", "so-github"]

    def test_keeps_the_username_found_in_another_source(self) -> None:
        a = gp.GitPerson(name="Ana", email="ana@x.com", commits=3)
        b = gp.GitPerson(name="Ana", email="ana@x.com", username="ana", source="github")

        [merged] = gp.merge_people([a], [b])

        assert merged.username == "ana"


class TestFilter:
    PEOPLE = [
        gp.GitPerson(name="Ana Souza", email="ana@x.com", username="anas"),
        gp.GitPerson(name="Bruno Lima", email="bruno@x.com"),
        gp.GitPerson(name="Carla", email="c@x.com", username="carla-dev"),
    ]

    def test_empty_query_lists_everyone(self) -> None:
        assert len(gp.filter_people(self.PEOPLE, "")) == 3

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("ana", ["Ana Souza"]),
            ("LIMA", ["Bruno Lima"]),
            ("@carla", ["Carla"]),
            ("c@x", ["Carla"]),
            ("zzz", []),
        ],
    )
    def test_matches_name_email_or_username(
        self, query: str, expected: list[str]
    ) -> None:
        assert [p.name for p in gp.filter_people(self.PEOPLE, query)] == expected

    def test_excludes_the_current_user(self) -> None:
        result = gp.filter_people(self.PEOPLE, "", exclude_emails={"BRUNO@x.com"})

        assert [p.name for p in result] == ["Ana Souza", "Carla"]

    def test_limits_the_result(self) -> None:
        many = [gp.GitPerson(name=f"P{i}", email=f"{i}@x.com") for i in range(50)]

        assert len(gp.filter_people(many, "", limit=5)) == 5


# --- rota ------------------------------------------------------------------


@pytest.fixture
def ws_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import git

    from backend.vtypes import Workspace
    from backend.workspace import workspace as ws_mod

    repo = git.Repo.init(tmp_path)
    repo.config_writer().set_value("user", "name", "Eu Mesmo").release()
    repo.config_writer().set_value("user", "email", "eu@x.com").release()
    (tmp_path / "a.txt").write_text("a\n")
    repo.index.add(["a.txt"])
    repo.index.commit("um")
    (tmp_path / "b.txt").write_text("b\n")
    repo.index.add(["b.txt"])
    repo.index.commit("dois", author=git.Actor("Ana Souza", "ana@example.com"))
    ws = Workspace(
        id="ws-people",
        name="ws-people",
        cwd=str(tmp_path),
        created_at="2024-01-01T00:00:00+00:00",
        trusted=True,
    )
    monkeypatch.setattr(ws_mod.workspace_registry, "get", lambda wid: ws)
    return repo


def _request() -> Request:
    from types import SimpleNamespace

    from starlette.requests import Request

    request = Request({"type": "http", "headers": [], "query_string": b""})
    request.state.user = SimpleNamespace(id="local")
    return request


@pytest.mark.asyncio
async def test_route_lists_committers_without_the_current_user(
    ws_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.api.handlers.workspaces import git_contributors
    from backend.services import github_cli

    async def no_gh(*_a: list[str], **_k: str) -> JsonObject:
        return {"status": "error", "message": "gh not found in PATH"}

    monkeypatch.setattr(github_cli, "run_gh_command", no_gh)

    people = await git_contributors("ws-people", _request(), "")

    assert [p.email for p in people] == ["ana@example.com"]


@pytest.mark.asyncio
async def test_route_adds_github_collaborators_and_filters(
    ws_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.api.handlers.workspaces import git_contributors
    from backend.services import github_cli

    calls: list[list[str]] = []

    async def fake_gh(args: list[str], **_k: str) -> JsonObject:
        calls.append(args)
        return {
            "status": "ok",
            "output": json.dumps([{"login": "carla", "id": 9, "type": "User"}]),
        }

    monkeypatch.setattr(github_cli, "run_gh_command", fake_gh)

    everyone = await git_contributors("ws-people", _request(), "")
    only_carla = await git_contributors("ws-people", _request(), "carl")

    assert {p.username for p in everyone} == {None, "carla"}
    assert [p.username for p in only_carla] == ["carla"]
    assert calls[0][0] == "api"


@pytest.mark.asyncio
async def test_route_finds_an_unknown_github_user_by_username(
    ws_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.api.handlers.workspaces import git_contributors
    from backend.services import github_cli

    async def fake_gh(args: list[str], **_k: str) -> JsonObject:
        if args[1].startswith("users/"):
            return {
                "status": "ok",
                "output": json.dumps({"login": "novato", "id": 77, "name": "Novo T"}),
            }
        return {"status": "ok", "output": "[]"}

    monkeypatch.setattr(github_cli, "run_gh_command", fake_gh)

    [person] = await git_contributors("ws-people", _request(), "@novato")

    assert (person.name, person.username) == ("Novo T", "novato")
    assert person.email == "77+novato@users.noreply.github.com"


@pytest.mark.asyncio
async def test_route_never_runs_gh_for_a_malicious_username(
    ws_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.api.handlers.workspaces import git_contributors
    from backend.services import github_cli

    seen: list[list[str]] = []

    async def fake_gh(args: list[str], **_k: str) -> JsonObject:
        seen.append(args)
        return {"status": "ok", "output": "[]"}

    monkeypatch.setattr(github_cli, "run_gh_command", fake_gh)

    result = await git_contributors("ws-people", _request(), "x;rm -rf /")

    assert result == []
    assert all(not a[1].startswith("users/") for a in seen)
