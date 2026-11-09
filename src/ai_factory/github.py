"""The few GitHub REST calls the API makes with the configured token: who it belongs
to, where it may create repositories, and creating one for a new project.

The token stays host-side, like everywhere else; these calls run in the API process.
"""

from __future__ import annotations

from typing import Any

import httpx

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200] or r.reason_phrase
    msg = str(body.get("message", "")) or r.reason_phrase
    details = [str(e.get("message", "")) for e in body.get("errors", []) if isinstance(e, dict)]
    return "; ".join([msg, *[d for d in details if d]])


async def user(token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/user", headers=_headers(token))
    if r.status_code == 401:
        raise GitHubError("GitHub rejected the token (401)")
    if r.is_error:
        raise GitHubError(f"GitHub: {_message(r)} ({r.status_code})")
    return {"login": r.json().get("login", ""), "scopes": r.headers.get("x-oauth-scopes")}


async def owners(token: str) -> list[str]:
    """The token's own account first, then the organisations it can see."""
    me = (await user(token))["login"]
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/user/orgs", headers=_headers(token), params={"per_page": 100})
    # Fine-grained tokens may not be allowed to list orgs; the account alone still works.
    orgs = [str(o["login"]) for o in r.json()] if r.is_success else []
    return [me, *orgs]


async def create_repo(
    token: str, owner: str, name: str, private: bool, description: str = ""
) -> dict[str, str]:
    me = (await user(token))["login"]
    url = f"{API}/user/repos" if owner.lower() == me.lower() else f"{API}/orgs/{owner}/repos"
    body = {
        "name": name,
        "private": private,
        "description": description,
        # A first commit on the default branch: the milestone pull request needs a base.
        "auto_init": True,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, headers=_headers(token), json=body)
    if r.status_code == 422:
        raise GitHubError(f"could not create {owner}/{name}: {_message(r)}")
    if r.status_code in (403, 404):
        raise GitHubError(
            f"the token may not create repositories for {owner} ({r.status_code}): {_message(r)}"
        )
    if r.is_error:
        raise GitHubError(f"GitHub: {_message(r)} ({r.status_code})")
    repo = r.json()
    return {
        "full_name": str(repo["full_name"]),
        "clone_url": str(repo["clone_url"]),
        "html_url": str(repo["html_url"]),
        "default_branch": str(repo.get("default_branch", "main")),
    }
