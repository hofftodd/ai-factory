"""Phase 1 end to end with a scripted planner model (validation.md V2.x, V3.x, E2E-1)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from sqlmodel import col, select

from foreman import gitops
from foreman.core.models import (
    Event,
    JobState,
    PlannerJob,
    ProjectPhase,
    SpecMessage,
    Task,
    TaskState,
)
from foreman.db import session
from foreman.orchestrator.main import Orchestrator, recover
from tests.conftest import git
from tests.fakellm import FakeLLM, last_user_text, system_text
from tests.helpers import TERMINAL, drive, project

FEATURE = "specs/001-initial"

SPEC = """# Spec: greeter
- FR-1.1 `greet(name)` returns "hello, <name>".
- FR-1.2 A CLI prints the greeting.
"""
PLAN = "# Plan\n- src/greet.py: greet()\n- src/cli.py: CLI\n"
VALIDATION = "# Validation\n- V1 (FR-1.1) greet('bob') == 'hello, bob'\n"

CHAT_REPLY = f"""I drafted everything.

<<<FILE {FEATURE}/spec.md
{SPEC}>>>
<<<FILE {FEATURE}/plan.md
{PLAN}>>>
<<<FILE {FEATURE}/validation.md
{VALIDATION}>>>
<<<FILE tests/acceptance/check.sh
test -f greet.txt
>>>
<<<ACCEPTANCE_CMD
sh tests/acceptance/check.sh
>>>
Anything else?"""

GOOD_TASKS = [
    {
        "key": "T1",
        "title": "Write greeter",
        "req_refs": ["FR-1.1"],
        "todos": ["create greet.txt"],
        "allowed_files": ["greet.txt"],
        "context_files": [],
        "validation_cmd": "true",
        "deps": [],
    },
    {
        "key": "T2",
        "title": "Write CLI",
        "req_refs": ["FR-1.2"],
        "todos": [],
        "allowed_files": ["cli.txt"],
        "context_files": [],
        "validation_cmd": "true",
        "deps": ["T1"],
    },
]
TOO_BIG = [{**GOOD_TASKS[0], "allowed_files": ["a.txt", "b.txt", "c.txt", "d.txt"]}]


def scripted(chat: str = CHAT_REPLY, decompose: list[Any] | Callable[[int], Any] = GOOD_TASKS):
    calls = {"decompose": 0}

    def planner(req: dict[str, Any]) -> str:
        if "You review the plan" in system_text(req):
            return json.dumps({"summary": "on track", "ops": []})
        if "Break the approved plan" in system_text(req):
            calls["decompose"] += 1
            out = decompose(calls["decompose"]) if callable(decompose) else decompose
            return out if isinstance(out, str) else "```json\n" + json.dumps(out) + "\n```"
        return chat

    return planner


@pytest.fixture
def llm():
    server = FakeLLM(host="127.0.0.1", planner=scripted()).start()
    yield server
    server.stop()


def spec_project(client, origin, llm, **kw) -> dict[str, Any]:
    n = client.post(
        "/api/nodes", json={"name": "planner", "base_url": llm.url, "context_window": 32768}
    ).json()
    body = {
        "name": "greeter",
        "repo_url": str(origin),
        "executor_kind": "shell",
        "role_bindings": {"planner": {"node_id": n["id"], "model": "fake"}},
        **kw,
    }
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["phase"] == "specify"
    return p


def job_states(pid: int) -> list[JobState]:
    with session() as s:
        return [
            j.state
            for j in s.exec(
                select(PlannerJob).where(PlannerJob.project_id == pid).order_by(col(PlannerJob.id))
            )
        ]


async def chat(client, orch, pid: int, text: str) -> None:
    r = client.post(f"/api/projects/{pid}/spec/messages", json={"content": text})
    assert r.status_code == 201, r.text
    await drive(orch, lambda: job_states(pid)[-1] not in (JobState.QUEUED, JobState.RUNNING))


async def test_chat_writes_and_commits_documents(client, origin, llm):
    p = spec_project(client, origin, llm)
    orch = Orchestrator()
    await chat(client, orch, p["id"], "Build a greeter.")
    assert job_states(p["id"]) == [JobState.DONE]

    spec = client.get(f"/api/projects/{p['id']}/spec").json()
    assert spec["docs"]["spec"].strip() == SPEC.strip()
    assert spec["acceptance_cmd"] == "sh tests/acceptance/check.sh"
    roles = [m["role"] for m in spec["messages"]]
    assert roles == ["user", "assistant"]
    reply = spec["messages"][1]
    assert "[wrote specs/001-initial/spec.md]" in reply["content"]
    assert "tests/acceptance/check.sh" in reply["files"]

    repo = gitops.repo_dir(p["id"])
    assert (repo / "tests/acceptance/check.sh").exists()
    log = git("log", "--format=%s", cwd=repo)
    assert "spec: I drafted everything." in log
    # The planner saw the user's message.
    assert last_user_text(llm.requests[-1]) == "Build a greeter."


async def test_second_turn_sees_current_documents(client, origin, llm):
    p = spec_project(client, origin, llm)
    orch = Orchestrator()
    await chat(client, orch, p["id"], "Build a greeter.")
    await chat(client, orch, p["id"], "Looks good.")
    sys = system_text(llm.requests[-1])
    assert "FR-1.1 `greet(name)`" in sys  # docs are in the system prompt
    roles = [m["role"] for m in llm.requests[-1]["messages"] if m["role"] != "system"]
    assert roles == ["user", "assistant", "user"]


def test_cannot_chat_while_planner_busy(client, origin, llm):
    p = spec_project(client, origin, llm)
    client.post(f"/api/projects/{p['id']}/spec/messages", json={"content": "a"})
    r = client.post(f"/api/projects/{p['id']}/spec/messages", json={"content": "b"})
    assert r.status_code == 409


async def test_user_can_edit_documents(client, origin, llm):
    p = spec_project(client, origin, llm)
    r = client.put(f"/api/projects/{p['id']}/spec/docs/plan", json={"content": "# My plan"})
    assert r.status_code == 200
    assert client.get(f"/api/projects/{p['id']}/spec").json()["docs"]["plan"] == "# My plan\n"
    assert (
        client.put(f"/api/projects/{p['id']}/spec/docs/tasks", json={"content": "x"}).status_code
        == 400
    )


async def test_spec_approval_requires_documents_and_command(client, origin, llm):
    p = spec_project(client, origin, llm)
    r = client.post(f"/api/projects/{p['id']}/spec/approve")
    assert r.status_code == 400 and "missing documents" in r.json()["detail"]
    for name in ("spec", "plan", "validation"):
        client.put(f"/api/projects/{p['id']}/spec/docs/{name}", json={"content": "x"})
    r = client.post(f"/api/projects/{p['id']}/spec/approve")
    assert r.status_code == 400 and "acceptance command" in r.json()["detail"]


async def test_full_flow_spec_to_done(client, origin, llm):
    """E2E-1 with the shell executor: chat -> approve -> tasks -> approve -> run -> done."""
    p = spec_project(client, origin, llm)
    pid = p["id"]
    orch = Orchestrator()
    await chat(client, orch, pid, "Build a greeter.")

    assert client.post(f"/api/projects/{pid}/spec/approve").status_code == 200
    assert project(pid).phase == ProjectPhase.DECOMPOSING
    assert project(pid).spec_commit
    await drive(orch, lambda: project(pid).phase != ProjectPhase.DECOMPOSING)
    assert project(pid).phase == ProjectPhase.PLAN_REVIEW

    with session() as s:
        tasks = s.exec(select(Task).where(Task.project_id == pid).order_by(col(Task.id))).all()
    assert [(t.key, t.state) for t in tasks] == [
        ("T1", TaskState.PROPOSED),
        ("T2", TaskState.PROPOSED),
    ]
    assert tasks[1].deps == [tasks[0].id]
    assert tasks[0].size_violations == [] and tasks[0].pack_tokens > 0
    tasks_md = client.get(f"/api/projects/{pid}/spec").json()["docs"]["tasks"]
    assert "## T1: Write greeter" in tasks_md and "Depends on: T1" in tasks_md

    # Proposed tasks never run before approval.
    for _ in range(3):
        await orch.tick()
    assert all(t.state == TaskState.PROPOSED for t in [task_row(t.id) for t in tasks])

    # Make T1 actually produce what the acceptance test checks.
    client.patch(
        f"/api/tasks/{tasks[0].id}",
        json={"executor_kind": "shell", "executor_script": "echo hi > greet.txt"},
    )
    r = client.post(f"/api/projects/{pid}/plan/approve")
    assert r.status_code == 200 and r.json()["queued"] == 2
    assert project(pid).phase == ProjectPhase.EXECUTE
    await drive(orch, lambda: task_row(tasks[1].id).state in TERMINAL)
    assert task_row(tasks[0].id).state == TaskState.DONE
    assert task_row(tasks[1].id).state == TaskState.DONE
    assert (gitops.repo_dir(pid) / "greet.txt").exists()


def task_row(task_id: int) -> Task:
    with session() as s:
        t = s.get(Task, task_id)
        assert t is not None
        return t


async def approved(client, orch, pid: int) -> None:
    await chat(client, orch, pid, "Build a greeter.")
    assert client.post(f"/api/projects/{pid}/spec/approve").status_code == 200
    await drive(orch, lambda: project(pid).phase != ProjectPhase.DECOMPOSING)


async def test_oversized_task_is_sent_back_to_be_split(client, origin, llm):
    llm.planner = scripted(decompose=lambda n: TOO_BIG if n == 1 else GOOD_TASKS)
    p = spec_project(client, origin, llm)
    await approved(client, Orchestrator(), p["id"])
    retry = last_user_text(llm.requests[-1])
    assert "touches 4 files (limit 3)" in retry and "COMPLETE corrected" in retry
    with session() as s:
        keys = [t.key for t in s.exec(select(Task).where(Task.project_id == p["id"]))]
    assert keys == ["T1", "T2"]


async def test_persistent_violations_block_plan_approval(client, origin, llm):
    llm.planner = scripted(decompose=TOO_BIG)
    p = spec_project(client, origin, llm)
    pid = p["id"]
    await approved(client, Orchestrator(), pid)
    assert project(pid).phase == ProjectPhase.PLAN_REVIEW
    with session() as s:
        t = s.exec(select(Task).where(Task.project_id == pid)).one()
    assert "touches 4 files (limit 3)" in t.size_violations
    r = client.post(f"/api/projects/{pid}/plan/approve")
    assert r.status_code == 400 and "size limits" in r.json()["detail"]

    # The user splits it by hand: editing re-runs the size check.
    fixed = client.patch(f"/api/tasks/{t.id}", json={"allowed_globs": ["a.txt"]}).json()
    assert fixed["size_violations"] == []
    assert client.post(f"/api/projects/{pid}/plan/approve").status_code == 200


async def test_unparseable_breakdown_returns_to_specify(client, origin, llm):
    llm.planner = scripted(decompose=lambda n: "Sorry, I can't do JSON.")
    p = spec_project(client, origin, llm)
    pid = p["id"]
    await approved(client, Orchestrator(), pid)
    assert project(pid).phase == ProjectPhase.SPECIFY
    assert job_states(pid)[-1] == JobState.FAILED
    with session() as s:
        last = s.exec(
            select(SpecMessage)
            .where(SpecMessage.project_id == pid)
            .order_by(col(SpecMessage.id).desc())
        ).first()
    assert last is not None and "Task breakdown did not complete" in last.content


async def test_reopen_and_redecompose(client, origin, llm):
    p = spec_project(client, origin, llm)
    pid = p["id"]
    orch = Orchestrator()
    await approved(client, orch, pid)
    assert client.post(f"/api/projects/{pid}/plan/redecompose").status_code == 200
    await drive(orch, lambda: project(pid).phase == ProjectPhase.PLAN_REVIEW)
    with session() as s:
        assert len(s.exec(select(Task).where(Task.project_id == pid)).all()) == 2
    assert client.post(f"/api/projects/{pid}/spec/reopen").status_code == 200
    assert project(pid).phase == ProjectPhase.SPECIFY
    with session() as s:
        assert s.exec(select(Task).where(Task.project_id == pid)).all() == []


async def test_stop_aborts_planner_mid_stream_and_applies_nothing(client, origin, llm):
    """FR-8.2 for planner calls: Stop closes the model stream; no files are written."""
    llm.planner = scripted(chat=CHAT_REPLY + "\n" + "x" * 20000)
    llm.chunk_delay = 0.02
    p = spec_project(client, origin, llm)
    pid = p["id"]
    orch = Orchestrator()
    client.post(f"/api/projects/{pid}/spec/messages", json={"content": "go"})
    await drive(
        orch, lambda: bool(llm.requests) and job_states(pid)[-1] == JobState.RUNNING, settle=False
    )
    client.post("/api/controls", json={"scope": "project", "target_id": pid, "action": "stop"})
    await drive(orch, lambda: job_states(pid)[-1] == JobState.STOPPED)
    assert llm.disconnected.wait(5)
    assert not (gitops.repo_dir(pid) / FEATURE / "spec.md").exists()


async def test_missing_planner_binding_fails_the_job(client, origin):
    r = client.post("/api/projects", json={"name": "x", "repo_url": str(origin)})
    pid = r.json()["id"]
    orch = Orchestrator()
    await chat(client, orch, pid, "hi")
    with session() as s:
        job = s.exec(select(PlannerJob).where(PlannerJob.project_id == pid)).one()
    assert job.state == JobState.FAILED and "no planner" in job.error


def test_cloud_nodes_cannot_be_executors(client, origin):
    n = client.post(
        "/api/nodes",
        json={
            "name": "claude",
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk",
        },
    ).json()
    r = client.post(
        "/api/projects",
        json={
            "name": "x",
            "repo_url": str(origin),
            "role_bindings": {"executor": {"node_id": n["id"], "model": "claude-opus-5-5"}},
        },
    )
    assert r.status_code == 400 and "not executors" in r.json()["detail"]


async def test_tasks_do_not_run_before_execute_phase(client, origin, llm):
    p = spec_project(client, origin, llm)
    t = client.post(
        f"/api/projects/{p['id']}/tasks", json={"title": "early", "validation_cmd": "true"}
    ).json()
    orch = Orchestrator()
    for _ in range(3):
        await orch.tick()
    assert task_row(t["id"]).state == TaskState.QUEUED


def test_recover_fails_interrupted_jobs(client, origin, llm):
    p = spec_project(client, origin, llm)
    with session() as s:
        s.add(PlannerJob(project_id=p["id"], kind="decompose", state=JobState.RUNNING))
        s.commit()
    from foreman.core.models import Project as ProjectRow

    with session() as s:
        row = s.get(ProjectRow, p["id"])
        row.phase = ProjectPhase.DECOMPOSING
        s.add(row)
        s.commit()
    recover()
    assert job_states(p["id"]) == [JobState.FAILED]
    assert project(p["id"]).phase == ProjectPhase.SPECIFY


async def test_failure_while_applying_results_is_reported(client, origin, llm, monkeypatch):
    p = spec_project(client, origin, llm)

    def broken(*a, **k):
        raise gitops.GitError("disk full")

    monkeypatch.setattr(gitops, "write_and_commit", broken)
    await chat(client, Orchestrator(), p["id"], "Build a greeter.")
    with session() as s:
        job = s.exec(select(PlannerJob).where(PlannerJob.project_id == p["id"])).one()
        last = s.exec(
            select(SpecMessage)
            .where(SpecMessage.project_id == p["id"])
            .order_by(col(SpecMessage.id).desc())
        ).first()
    assert job.state == JobState.FAILED and "disk full" in job.error
    assert last is not None and "planner error" in last.content


async def test_paused_project_holds_planner_jobs(client, origin, llm):
    p = spec_project(client, origin, llm)
    client.post("/api/controls", json={"scope": "project", "target_id": p["id"], "action": "pause"})
    client.post(f"/api/projects/{p['id']}/spec/messages", json={"content": "hi"})
    orch = Orchestrator()
    for _ in range(3):
        await orch.tick()
        await asyncio.sleep(0.2)  # let anything that was started actually run
    assert job_states(p["id"]) == [JobState.QUEUED]
    client.post(
        "/api/controls", json={"scope": "project", "target_id": p["id"], "action": "resume"}
    )
    await drive(orch, lambda: job_states(p["id"]) == [JobState.DONE])


async def test_plan_approval_rejects_dependencies_on_dropped_tasks(client, origin, llm):
    p = spec_project(client, origin, llm)
    pid = p["id"]
    await approved(client, Orchestrator(), pid)
    with session() as s:
        t1 = s.exec(select(Task).where(Task.project_id == pid, Task.key == "T1")).one()
    client.post(f"/api/tasks/{t1.id}/discard")
    r = client.post(f"/api/projects/{pid}/plan/approve")
    assert r.status_code == 400 and "dropped tasks: T2" in r.json()["detail"]


async def test_runs_to_done_without_any_input_when_optional_gates_are_off(client, origin, llm):
    """V7.1: after spec approval, three tasks run, are reviewed, pass acceptance and the
    project finishes without a single checkpoint."""
    chat_reply = CHAT_REPLY.replace("sh tests/acceptance/check.sh", "test -f c.txt")
    three = [
        {**GOOD_TASKS[0], "allowed_files": ["a.txt"]},
        {**GOOD_TASKS[1], "allowed_files": ["b.txt"]},
        {
            "key": "T3",
            "title": "Third",
            "allowed_files": ["c.txt"],
            "validation_cmd": "true",
            "deps": ["T2"],
        },
    ]
    llm.planner = scripted(chat=chat_reply, decompose=three)
    p = spec_project(client, origin, llm, gates={"plan_approval": False, "milestone": False})
    pid = p["id"]
    orch = Orchestrator()
    await chat(client, orch, pid, "Build it.")
    assert client.post(f"/api/projects/{pid}/spec/approve").status_code == 200
    await drive(orch, lambda: project(pid).phase == ProjectPhase.EXECUTE)
    # The shell executor stands in for the coding agent: each task writes its file.
    with session() as s:
        for t in s.exec(select(Task).where(Task.project_id == pid)):
            t.executor_script = f"echo ok > {t.allowed_globs[0]}"
            s.add(t)
        s.commit()
    await drive(orch, lambda: project(pid).phase == ProjectPhase.DONE, timeout=60)
    with session() as s:
        from foreman.core.models import Gate, PlanRevision

        assert s.exec(select(Gate).where(Gate.project_id == pid)).all() == []
        reviews = s.exec(select(PlanRevision).where(PlanRevision.project_id == pid)).all()
    assert len(reviews) == 3  # one after each task
    assert project(pid).acceptance_passed and project(pid).baseline_tasks == 3


async def test_thinking_model_streams_reasoning_and_gets_room_to_think(client, origin, llm):
    llm.reasoning = "Let me think about the greeter. " * 40
    p = spec_project(client, origin, llm)
    await chat(client, Orchestrator(), p["id"], "Build a greeter.")
    assert job_states(p["id"]) == [JobState.DONE]
    reply = client.get(f"/api/projects/{p['id']}/spec").json()["messages"][1]["content"]
    assert "Let me think" not in reply  # reasoning is never part of the reply
    with session() as s:
        thinking = s.exec(select(Event).where(Event.kind == "planner_thinking")).all()
    assert "".join(e.message for e in thinking) == llm.reasoning
    # The output budget is what the 32k context leaves, not a fixed 16k.
    assert 16000 < llm.requests[-1]["max_tokens"] < 32768


async def test_planner_cut_off_while_thinking_says_so(client, origin, llm):
    llm.reasoning = "Hmm. " * 20
    llm.planner = lambda req: ""
    llm.finish = "length"
    p = spec_project(client, origin, llm)
    await chat(client, Orchestrator(), p["id"], "Build a greeter.")
    with session() as s:
        job = s.exec(select(PlannerJob).where(PlannerJob.project_id == p["id"])).one()
    assert job.state == JobState.FAILED
    assert "output budget thinking without answering" in job.error
