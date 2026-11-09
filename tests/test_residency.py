"""Swap-aware scheduling on ai-swap nodes (ai_factory.residency)."""

from __future__ import annotations

import time

import pytest
from sqlmodel import col, select

from ai_factory import residency
from ai_factory.core.models import Event, Node, Project, RunState, Task, TaskRun, TaskState
from ai_factory.db import session
from ai_factory.orchestrator.main import pick_ready
from tests.helpers import make_project, make_task

SWAP = "http://swap.test:8080"


def add_node(client, name: str = "swap", capacity: int = 4) -> int:
    r = client.post(
        "/api/nodes", json={"name": name, "base_url": SWAP + "/v1", "capacity": capacity}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def node(node_id: int) -> Node:
    with session() as s:
        n = s.get(Node, node_id)
        assert n is not None
        return n


def cluster(budget: float, **models: tuple[str, float, str]) -> None:
    """One ai-swap machine "gpu" with models name=(state, memory_gb, last_used)."""
    status = {
        "node": {
            "name": "gpu",
            "memory_budget_gb": budget,
            "models": [
                {"name": n, "state": st, "memory_gb": mem, "inflight": 0, "last_used": lu}
                for n, (st, mem, lu) in models.items()
            ],
        },
        "peers": [],
    }
    listed = {"data": [{"id": n, "node": "gpu"} for n in models]}
    residency._views[SWAP] = residency._parse(status, listed)


def running(project_id: int, node_id: int, model: str, step: str = "builder") -> int:
    with session() as s:
        t = Task(project_id=project_id, title="busy", state=TaskState.RUNNING)
        s.add(t)
        s.commit()
        run = TaskRun(
            task_id=t.id or 0, project_id=project_id, node_id=node_id, model=model, step=step
        )
        s.add(run)
        s.commit()
        assert run.id is not None
        return run.id


def finish(run_id: int) -> None:
    with session() as s:
        run = s.get(TaskRun, run_id)
        assert run is not None
        run.state = RunState.SUCCEEDED
        s.add(run)
        s.commit()


def project_on(client, origin, nid: int, model: str, name: str = "p") -> dict:
    return make_project(
        client,
        origin,
        name=name,
        executor_kind="opencode",
        role_bindings={r: {"node_id": nid, "model": model} for r in ("tester", "builder")},
    )


def events(text: str) -> list[Event]:
    with session() as s:
        return [e for e in s.exec(select(Event).order_by(col(Event.id))) if text in e.message]


# -- the decision ----------------------------------------------------------------------


def test_fits_in_free_memory_is_fine(client):
    nid = add_node(client)
    cluster(220, flint=("ready", 36, "1"), deepseek=("stopped", 175, ""))
    busy = {("gpu", "flint"): "p's builder"}
    assert residency.blocker(node(nid), "deepseek", busy) == ""


def test_swapping_out_a_model_in_use_waits(client):
    nid = add_node(client)
    cluster(32, thinkingcap=("ready", 30, "1"), gemma=("stopped", 30, ""))
    reason = residency.blocker(node(nid), "gemma", {("gpu", "thinkingcap"): "alpha's tester"})
    assert "would swap out thinkingcap (alpha's tester)" in reason
    # Nobody using it: a plain swap, which is fine.
    assert residency.blocker(node(nid), "gemma", {}) == ""


def test_eviction_follows_ai_swap_lru_order(client):
    """ai-swap unloads the least recently used idle model, even if another idle model would
    do: an agent between turns looks idle to it, so ai-factory must check the actual victim."""
    nid = add_node(client)
    cluster(
        220,
        a=("ready", 100, "2026-01-01T00:00:00Z"),
        c=("ready", 100, "2026-01-02T00:00:00Z"),
        b=("stopped", 100, ""),
    )
    assert "would swap out a" in residency.blocker(node(nid), "b", {("gpu", "a"): "x"})
    # If the in-use model is the more recently used one, the LRU victim is c: fine.
    assert residency.blocker(node(nid), "b", {("gpu", "c"): "x"}) == ""


def test_models_already_loaded_need_no_swap(client):
    nid = add_node(client)
    cluster(32, thinkingcap=("ready", 30, "1"))
    assert residency.warm(node(nid), "thinkingcap")
    assert residency.blocker(node(nid), "thinkingcap", {("gpu", "thinkingcap"): "x"}) == ""


def test_reserved_machine_makes_work_wait(client):
    nid = add_node(client)
    cluster(32, thinkingcap=("ready", 30, "1"))
    residency._views[SWAP].machines["gpu"].reserved = "ai-bench@mini (run 7)"
    assert (
        residency.blocker(node(nid), "thinkingcap", {})
        == "gpu is reserved by ai-bench@mini (run 7)"
    )
    assert not residency.warm(node(nid), "thinkingcap")


def test_parse_reads_the_reservation():
    status = {
        "node": {
            "name": "gpu",
            "memory_budget_gb": 32,
            "models": [],
            "lock": {"owner": "ai-bench@mini", "reason": "run 7"},
        },
        "peers": [],
    }
    assert (
        residency._parse(status, {"data": []}).machines["gpu"].reserved == "ai-bench@mini (run 7)"
    )


def test_plain_nodes_are_left_alone(client):
    r = client.post("/api/nodes", json={"name": "plain", "base_url": "http://127.0.0.1:9/v1"})
    n = node(r.json()["id"])
    assert residency.blocker(n, "anything", {}) == "" and residency.warm(n, "anything")


def test_parse_status_with_peers_and_duplicate_names():
    status = {
        "node": {
            "name": "studio",
            "memory_budget_gb": 220,
            "models": [{"name": "flint", "state": "ready", "memory_gb": 36}],
        },
        "peers": [
            {
                "online": True,
                "node": {
                    "name": "mini",
                    "memory_budget_gb": 32,
                    "models": [{"name": "flint", "state": "stopped", "memory_gb": 30}],
                },
            },
            {"online": False, "node": {"name": "gone", "models": []}},
        ],
    }
    listed = {"data": [{"id": "flint", "node": "studio"}, {"id": "flint@mini", "node": "mini"}]}
    v = residency._parse(status, listed)
    assert set(v.machines) == {"studio", "mini"}
    assert v.ids["flint"] == ("studio", "flint") and v.ids["flint@mini"] == ("mini", "flint")


# -- scheduling ---------------------------------------------------------------------------


def test_task_waits_instead_of_swapping_out_a_running_step(client, origin):
    nid = add_node(client)
    cluster(32, thinkingcap=("ready", 30, "1"), gemma=("stopped", 30, ""))
    other = project_on(client, origin, nid, "thinkingcap", name="alpha")
    run = running(other["id"], nid, "thinkingcap", step="tester")
    p = project_on(client, origin, nid, "gemma", name="beta")
    make_task(client, p["id"], script="")

    assert pick_ready() == []
    assert pick_ready() == []
    waits = events("waiting to start")
    assert len(waits) == 1  # announced once
    assert "thinkingcap (alpha's tester)" in waits[0].message

    finish(run)
    assert [t.project_id for t, _, _ in pick_ready()] == [p["id"]]


def test_loaded_model_goes_first(client, origin):
    nid = add_node(client, capacity=1)
    cluster(32, thinkingcap=("ready", 30, "1"), gemma=("stopped", 30, ""))
    cold = project_on(client, origin, nid, "gemma", name="cold")
    warm = project_on(client, origin, nid, "thinkingcap", name="warm")
    make_task(client, cold["id"], script="")  # lower id, but its model isn't loaded
    make_task(client, warm["id"], script="")
    assert [t.project_id for t, _, _ in pick_ready()] == [warm["id"]]


def test_two_cold_models_that_dont_fit_together_start_one_at_a_time(client, origin):
    nid = add_node(client)
    cluster(32, gemma=("stopped", 30, ""), flint=("stopped", 30, ""))
    a = project_on(client, origin, nid, "gemma", name="a")
    b = project_on(client, origin, nid, "flint", name="b")
    make_task(client, a["id"], script="")
    make_task(client, b["id"], script="")
    started = pick_ready()
    assert len(started) == 1  # the second would need the first's memory


def test_waiting_has_a_limit(client, origin, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_SWAP_WAIT_S", "1")
    nid = add_node(client)
    cluster(32, thinkingcap=("ready", 30, "1"), gemma=("stopped", 30, ""))
    other = project_on(client, origin, nid, "thinkingcap", name="alpha")
    running(other["id"], nid, "thinkingcap")
    p = project_on(client, origin, nid, "gemma", name="beta")
    make_task(client, p["id"], script="")
    assert pick_ready() == []
    time.sleep(1.1)
    assert [t.project_id for t, _, _ in pick_ready()] == [p["id"]]


# -- settings warning -----------------------------------------------------------------------


@pytest.fixture
def fake_fetch(monkeypatch):
    async def fetch(n, client):
        return residency._views.get(residency.root(n)) if residency.root(n) == SWAP else None

    monkeypatch.setattr(residency, "fetch", fetch)


def test_conflicting_bindings_are_reported(client, fake_fetch):
    nid = add_node(client)
    cluster(
        32, thinkingcap=("stopped", 30, ""), gemma=("stopped", 30, ""), small=("stopped", 2, "")
    )
    r = client.post(
        "/api/residency/conflicts",
        json={
            "role_bindings": {
                "tester": {"node_id": nid, "model": "thinkingcap"},
                "builder": {"node_id": nid, "model": "gemma"},
            }
        },
    )
    assert r.status_code == 200
    (w,) = r.json()["warnings"]
    assert "On gpu" in w and "tester: thinkingcap (30 GB)" in w and "builder: gemma (30 GB)" in w

    ok = client.post(
        "/api/residency/conflicts",
        json={
            "role_bindings": {
                "tester": {"node_id": nid, "model": "thinkingcap"},
                "builder": {"node_id": nid, "model": "thinkingcap"},
                "project_planner": {"node_id": nid, "model": "small"},
            }
        },
    )
    assert ok.json()["warnings"] == []


def test_machines_group_nodes_by_the_computer_serving_their_model(client, origin, fake_fetch):
    """The sidebar's Machines: ai-swap's computers, each with the nodes whose model it serves."""
    status = {
        "node": {
            "name": "studio",
            "memory_budget_gb": 220,
            "models": [{"name": "flint", "state": "ready", "memory_gb": 36}],
        },
        "peers": [
            {
                "online": True,
                "node": {
                    "name": "mini",
                    "memory_budget_gb": 32,
                    "lock": {"owner": "ai-bench", "reason": "speed run"},
                    "models": [{"name": "thinkingcap", "state": "stopped", "memory_gb": 20}],
                },
            }
        ],
    }
    listed = {"data": [{"id": "flint", "node": "studio"}, {"id": "thinkingcap", "node": "mini"}]}
    residency._views[SWAP] = residency._parse(status, listed)
    # As after a health check: every node lists everything the endpoint serves.
    everything = ["flint", "thinkingcap"]
    ids = {}
    for name in ("flint", "thinkingcap", "new", "bound"):
        r = client.post(
            "/api/nodes", json={"name": name, "base_url": SWAP + "/v1", "models": everything}
        )
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    # A project's binding says which model a node is for, whatever its name.
    pid = make_project(client, origin, name="p")["id"]
    with session() as s:
        p = s.get(Project, pid)
        assert p is not None
        p.role_bindings = {"tester": {"node_id": ids["bound"], "model": "thinkingcap"}}
        s.add(p)
        s.commit()
    r = client.post("/api/nodes", json={"name": "plain", "base_url": "http://box.lan:8000/v1"})
    assert r.status_code == 201, r.text

    got = {m["name"]: m for m in client.get("/api/machines").json()}
    assert set(got) == {"studio", "mini", "box.lan"}
    studio, mini = got["studio"], got["mini"]
    # Named like a model: that model. Neither named nor bound: the first one listed.
    assert [n["name"] for n in studio["nodes"]] == ["flint", "new"]
    assert studio["nodes"][0]["model_state"] == "ready" and studio["used_gb"] == 36
    assert [(n["name"], n["model"]) for n in mini["nodes"]] == [
        ("thinkingcap", "thinkingcap"),
        ("bound", "thinkingcap"),
    ]
    assert mini["reserved"] == "ai-bench (speed run)" and mini["budget_gb"] == 32
    assert got["box.lan"]["ai_swap"] is False and [n["name"] for n in got["box.lan"]["nodes"]] == [
        "plain"
    ]
