from __future__ import annotations

import json

import httpx
import pytest

from ai_factory import github


@pytest.fixture
def fake_github(monkeypatch):
    """A scripted api.github.com: records requests, answers from `routes`."""
    calls: list[httpx.Request] = []
    routes: dict[tuple[str, str], httpx.Response] = {
        ("GET", "/user"): httpx.Response(200, json={"login": "octocat"}),
        ("GET", "/user/orgs"): httpx.Response(200, json=[{"login": "acme"}]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return routes.get((request.method, request.url.path), httpx.Response(404, json={}))

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return routes, calls


def _created(full_name: str) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "full_name": full_name,
            "clone_url": f"https://github.com/{full_name}.git",
            "html_url": f"https://github.com/{full_name}",
            "default_branch": "main",
        },
    )


def test_owners_lists_user_then_orgs(client, fake_github):
    client.put("/api/settings/github", json={"token": "t"})
    assert client.get("/api/github/owners").json() == ["octocat", "acme"]


def test_owners_without_org_access(client, fake_github):
    routes, _ = fake_github
    routes[("GET", "/user/orgs")] = httpx.Response(403, json={"message": "forbidden"})
    client.put("/api/settings/github", json={"token": "t"})
    assert client.get("/api/github/owners").json() == ["octocat"]


def test_create_repo_for_user_and_org(client, fake_github):
    routes, calls = fake_github
    routes[("POST", "/user/repos")] = _created("octocat/app")
    routes[("POST", "/orgs/acme/repos")] = _created("acme/app")
    assert (
        client.post("/api/github/repos", json={"owner": "octocat", "name": "app"}).status_code
        == 400
    )
    client.put("/api/settings/github", json={"token": "secret"})

    r = client.post("/api/github/repos", json={"owner": "OctoCat", "name": "app"})
    assert r.status_code == 201 and r.json()["clone_url"] == "https://github.com/octocat/app.git"
    sent = json.loads(calls[-1].content)
    assert sent == {"name": "app", "private": True, "description": "", "auto_init": True}
    assert calls[-1].headers["authorization"] == "Bearer secret"

    r = client.post("/api/github/repos", json={"owner": "acme", "name": "app", "private": False})
    assert r.json()["full_name"] == "acme/app" and calls[-1].url.path == "/orgs/acme/repos"


def test_create_repo_errors(client, fake_github):
    routes, _ = fake_github
    client.put("/api/settings/github", json={"token": "t"})
    routes[("POST", "/user/repos")] = httpx.Response(
        422,
        json={
            "message": "Repository creation failed.",
            "errors": [{"message": "name already exists"}],
        },
    )
    r = client.post("/api/github/repos", json={"owner": "octocat", "name": "app"})
    assert r.status_code == 400 and "name already exists" in r.json()["detail"]
    routes[("POST", "/orgs/acme/repos")] = httpx.Response(403, json={"message": "nope"})
    r = client.post("/api/github/repos", json={"owner": "acme", "name": "app"})
    assert "may not create repositories for acme" in r.json()["detail"]
    assert (
        client.post("/api/github/repos", json={"owner": "o", "name": "bad name"}).status_code == 422
    )


async def test_user_rejected_token(fake_github):
    routes, _ = fake_github
    routes[("GET", "/user")] = httpx.Response(401, json={"message": "Bad credentials"})
    with pytest.raises(github.GitHubError, match="401"):
        await github.user("t")
