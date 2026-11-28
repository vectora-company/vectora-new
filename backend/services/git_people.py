"""Pessoas do repositório para marcar como co-autor de um commit.

Junta quem já commitou (``git shortlog``) com os colaboradores do GitHub
(``gh``), quando disponível, e permite achar um usuário do GitHub pelo nome de
usuário mesmo que ele nunca tenha commitado ali.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from backend.vtypes.contracts import JsonValue

_SHORTLOG_LINE = re.compile(r"^\s*(\d+)\s+(.*?)\s*<([^>]+)>\s*$")
_NOREPLY = re.compile(
    r"^(?:(\d+)\+)?([^@]+)@users\.noreply\.github\.com$", re.IGNORECASE
)
_USERNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_DEFAULT_LIMIT = 20


class GitPerson(BaseModel):
    """Alguém que pode ser listado em ``Co-authored-by``."""

    name: str
    email: str
    username: str | None = None
    source: str = "git"  # "git" (já commitou) ou "github" (colaborador)
    commits: int = 0


def is_bot(name: str, email: str) -> bool:
    lowered = f"{name} {email}".lower()
    return "[bot]" in lowered or lowered.endswith("noreply@github.com")


def username_from_email(email: str) -> str | None:
    match = _NOREPLY.match(email.strip())
    return match.group(2) if match else None


def parse_shortlog(text: str) -> list[GitPerson]:
    """Lê a saída de ``git shortlog -sne``: ``  12\\tNome <email>``."""
    people: list[GitPerson] = []
    for line in text.splitlines():
        match = _SHORTLOG_LINE.match(line)
        if not match:
            continue
        count, name, email = int(match.group(1)), match.group(2), match.group(3)
        if is_bot(name, email):
            continue
        people.append(
            GitPerson(
                name=name or email.split("@")[0],
                email=email,
                username=username_from_email(email),
                commits=count,
            )
        )
    return people


def github_person(login: str, user_id: int | str, name: str | None = None) -> GitPerson:
    """Pessoa do GitHub com o e-mail noreply, que o GitHub reconhece como co-autor."""
    return GitPerson(
        name=name or login,
        email=f"{user_id}+{login}@users.noreply.github.com",
        username=login,
        source="github",
    )


def parse_gh_contributors(raw: str) -> list[GitPerson]:
    """Lê a saída JSON de ``gh api repos/:owner/:repo/contributors``."""
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    people: list[GitPerson] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("type") == "Bot":
            continue
        login, user_id = item.get("login"), item.get("id")
        if isinstance(login, str) and user_id and not is_bot(login, ""):
            people.append(github_person(login, user_id))
    return people


def parse_gh_user(raw: str) -> GitPerson | None:
    """Lê a saída JSON de ``gh api users/<login>``."""
    try:
        data: JsonValue = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") == "Bot":
        return None
    login, user_id = data.get("login"), data.get("id")
    if not isinstance(login, str) or not isinstance(user_id, int | str):
        return None
    if not user_id:
        return None
    name = data.get("name")
    return github_person(login, user_id, name if isinstance(name, str) else None)


def valid_username(candidate: str) -> bool:
    return bool(_USERNAME.match(candidate))


def merge_people(*groups: list[GitPerson]) -> list[GitPerson]:
    """Une as listas sem repetir a mesma pessoa (por e-mail ou usuário do GitHub).

    Quem já commitou vem primeiro, do mais ativo ao menos; depois os
    colaboradores do GitHub. Ao repetir, preserva o nome real e o usuário.
    """
    merged: list[GitPerson] = []
    by_email: dict[str, GitPerson] = {}
    by_login: dict[str, GitPerson] = {}
    for group in groups:
        for person in group:
            key = person.email.lower()
            login = (person.username or "").lower()
            existing = by_email.get(key) or (by_login.get(login) if login else None)
            if existing is not None:
                if not existing.username and person.username:
                    existing.username = person.username
                if existing.name == existing.email and person.name:
                    existing.name = person.name
                existing.commits = max(existing.commits, person.commits)
                continue
            by_email[key] = person
            if login:
                by_login[login] = person
            merged.append(person)
    return sorted(merged, key=lambda p: (p.source != "git", -p.commits, p.name.lower()))


def filter_people(
    people: list[GitPerson],
    query: str,
    *,
    exclude_emails: set[str] | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[GitPerson]:
    """Filtra por nome, e-mail ou usuário (sem acento nem caixa) e limita."""
    needle = query.strip().lstrip("@").lower()
    excluded = {e.lower() for e in (exclude_emails or set())}
    result: list[GitPerson] = []
    for person in people:
        if person.email.lower() in excluded:
            continue
        haystack = f"{person.name} {person.email} {person.username or ''}".lower()
        if needle and needle not in haystack:
            continue
        result.append(person)
        if len(result) >= limit:
            break
    return result
