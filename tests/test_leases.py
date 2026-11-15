"""ai-swap leases on a step's model, and the ai-swap API key (ai_factory.leases)."""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any

import pytest
from sqlmodel import select

from ai_factory import leases, residency
from ai_factory.core.models import (
    JobKind,
    JobState,
    Node,
    PlannerJob,
    Project,
    RunState,
    Task,
    TaskRun,
    TaskState,
)
from ai_factory.db import session
from ai_factory.orchestrator.main import recover

STATUS = {
    "node": {
        "name": "gpu",
        "memory_budget_gb": 32,
        "models": [{"name": "flint", "state": "ready", "memory_gb": 10}],
    },
    "peers": [],
}
MODELS = {"data": [{"id": "flint", "node": "gpu"}]}


class FakeSwap:
    """A stand-in ai-swap: /api/status, /v1/models and the lease API, recording requests."""

    def __init__(self, lease_status: int = 200):
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.lease_status = lease_status
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _reply(self, code: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _record(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                return body

            def do_GET(self):  # noqa: N802
                self._record()
                if self.path == "/api/status":
                    self._reply(200, STATUS)
                elif self.path == "/v1/models":
                    self._reply(200, MODELS)
                else:
                    self._reply(404, {"error": "no"})

            def do_POST(self):  # noqa: N802
                self._record()
                if outer.lease_status != 200:
                    self._reply(outer.lease_status, {"error": "no capacity"})
                else:
                    self._reply(200, {"lease_id": "L1", "lease": {"expires": "x"}, "node": "gpu"})

            def do_DELETE(self):  # noqa: N802
                self._record()
                self._reply(200, {"released": True})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def calls(self, method: str) -> list[tuple[str, dict[str, str], Any]]:
        return [(p, h, b) for m, p, h, b in self.requests if m == method]


@pytest.fixture
def swap():
    s = FakeSwap()
    yield s
    s.server.shutdown()


def add_node(url: str, api_key: str = "") -> Node:
    with session() as s:
        n = Node(name="flint", base_url=url + "/v1", api_key=api_key, models=["flint"])
        s.add(n)
        s.commit()
        s.refresh(n)
        return n


async def seen(node: Node) -> None:
    await residency.refresh([node])
    assert residency.view_for(node) is not None


async def test_a_step_holds_a_lease_and_releases_it(swap):
    node = add_node(swap.url)
    await seen(node)
    recorded: list[str] = []
    async with leases.hold(
        node, "flint", leases.owner("demo", "T1"), "builder step", recorded.append
    ) as held:
        assert held is not None and held.id == "L1"
        assert swap.calls("DELETE") == []
    ((_, _, body),) = swap.calls("POST")
    assert body["model"] == "flint" and body["owner"] == "ai-factory/demo/T1"
    assert body["ttl_seconds"] == 600 and body["reason"] == "builder step"
    ((path, _, _),) = swap.calls("DELETE")
    assert path == "/api/leases/L1?model=flint"
    assert recorded == ["L1", ""]


async def test_a_failing_step_releases_its_lease(swap):
    node = add_node(swap.url)
    await seen(node)
    with pytest.raises(RuntimeError):
        async with leases.hold(node, "flint", "o"):
            raise RuntimeError("step failed")
    assert len(swap.calls("DELETE")) == 1


async def test_nodes_that_arent_ai_swap_get_no_lease(swap):
    node = add_node(swap.url)  # never refreshed: residency doesn't know it as ai-swap
    async with leases.hold(node, "flint", "o") as held:
        assert held is None
    assert swap.calls("POST") == []


async def test_a_refused_lease_doesnt_stop_the_step():
    s = FakeSwap(lease_status=409)
    try:
        node = add_node(s.url)
        await seen(node)
        ran = False
        async with leases.hold(node, "flint", "o") as held:
            ran = True
        assert ran and held is None and s.calls("DELETE") == []
    finally:
        s.server.shutdown()


async def test_the_global_key_goes_where_a_node_has_none(swap, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_SWAP_API_KEY", "global")
    node = add_node(swap.url)
    await seen(node)
    async with leases.hold(node, "flint", "o"):
        pass
    auth = {(m, p.split("?")[0]): h.get("Authorization") for m, p, h, _ in swap.requests}
    assert auth == {
        ("GET", "/api/status"): "Bearer global",
        ("GET", "/v1/models"): "Bearer global",
        ("POST", "/api/leases"): "Bearer global",
        ("DELETE", "/api/leases/L1"): "Bearer global",
    }


async def test_a_nodes_own_key_wins(swap, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_SWAP_API_KEY", "global")
    node = add_node(swap.url, api_key="own")
    await seen(node)
    assert {h.get("Authorization") for _, _, h, _ in swap.requests} == {"Bearer own"}


def test_claude_nodes_never_get_the_swap_key(monkeypatch):
    monkeypatch.setenv("AI_FACTORY_SWAP_API_KEY", "global")
    assert residency.api_key(Node(name="c", provider="anthropic", base_url="x")) == ""
    assert residency.api_key(Node(name="l", base_url="x")) == "global"


def test_restart_recovery_releases_leases_left_behind(swap):
    node = add_node(swap.url)
    with session() as s:
        p = Project(name="demo", repo_url="x")
        s.add(p)
        s.commit()
        assert p.id is not None
        t = Task(project_id=p.id, title="t", state=TaskState.RUNNING)
        s.add(t)
        s.commit()
        assert t.id is not None
        s.add(
            TaskRun(
                task_id=t.id,
                project_id=p.id,
                node_id=node.id,
                model="flint",
                state=RunState.RUNNING,
                lease_id="L7",
            )
        )
        s.add(
            PlannerJob(
                project_id=p.id,
                kind=JobKind.CHAT,
                state=JobState.RUNNING,
                node_id=node.id,
                model="flint",
                lease_id="L8",
            )
        )
        s.commit()
    recover()
    paths = sorted(p for p, _, _ in swap.calls("DELETE"))
    assert paths == ["/api/leases/L7?model=flint", "/api/leases/L8?model=flint"]


def test_release_is_best_effort():
    node = Node(name="gone", base_url="http://127.0.0.1:9/v1")
    leases.release(node, "flint", "L1")  # nothing listens: logged, not raised


async def test_a_planner_job_on_ai_swap_holds_a_lease(client, origin):
    from tests.fakellm import FakeLLM
    from tests.helpers import drive

    llm = FakeLLM(host="127.0.0.1", planner=lambda r: "Noted.").start()
    try:
        llm.swap_status = {"node": {"name": "studio", "models": []}, "peers": []}
        llm.swap_models = [{"id": "fake", "node": "studio", "status": "ready"}]
        n = client.post("/api/nodes", json={"name": "planner", "base_url": llm.url}).json()
        binding = {"node_id": n["id"], "model": "fake"}
        p = client.post(
            "/api/projects",
            json={
                "name": "greeter",
                "repo_url": str(origin),
                "executor_kind": "shell",
                "role_bindings": {"project_planner": binding, "task_planner": binding},
            },
        ).json()
        from ai_factory.orchestrator.main import Orchestrator

        orch = Orchestrator()
        await residency.refresh()
        r = client.post(f"/api/projects/{p['id']}/spec/messages", json={"content": "hi"})
        assert r.status_code == 201, r.text

        def finished() -> bool:
            with session() as s:
                jobs = s.exec(select(PlannerJob)).all()
            return bool(jobs) and all(
                j.state not in (JobState.QUEUED, JobState.RUNNING) for j in jobs
            )

        await drive(orch, finished)
        (m1, _, body), (m2, path, _) = llm.leases
        assert (m1, m2) == ("POST", "DELETE")
        assert body["owner"] == "ai-factory/greeter/planner" and body["model"] == "fake"
        assert path == "/api/leases/L1?model=fake"
        with session() as s:
            assert all(j.lease_id == "" for j in s.exec(select(PlannerJob)))
    finally:
        llm.stop()


async def test_a_task_step_records_its_lease_on_the_run(swap):
    from ai_factory.orchestrator.runner import TaskRunner

    node = add_node(swap.url)
    await seen(node)
    with session() as s:
        p = Project(name="demo", repo_url="x")
        s.add(p)
        s.commit()
        assert p.id is not None
        t = Task(project_id=p.id, title="t", key="T3", state=TaskState.RUNNING)
        s.add(t)
        s.commit()
        assert t.id is not None
        run = TaskRun(task_id=t.id, project_id=p.id, node_id=node.id, model="flint")
        s.add(run)
        s.commit()
        assert run.id is not None
        task_id, run_id = t.id, run.id
    runner = TaskRunner(task_id, run_id)

    def lease_id() -> str:
        with session() as s:
            r = s.get(TaskRun, run_id)
            assert r is not None
            return r.lease_id

    async with runner._lease(node, "flint", "tester"):
        assert lease_id() == "L1"
    assert lease_id() == ""
    ((_, _, body),) = swap.calls("POST")
    assert body["owner"] == "ai-factory/demo/T3" and body["reason"] == "tester step"
    assert len(swap.calls("DELETE")) == 1
