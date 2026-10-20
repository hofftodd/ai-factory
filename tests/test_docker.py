"""Executor containers (validation.md V4.1, V4.4, V8.1 with Docker).

Needs a Docker daemon and the foreman/executor-base image:
    foreman build-image
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from foreman.core.models import TaskState
from foreman.orchestrator.main import Orchestrator
from foreman.sandbox import running_containers
from tests.helpers import TERMINAL, drive, make_project, make_task, task, wait_for

IMAGE = os.environ.get("FOREMAN_TEST_IMAGE", "foreman/executor-base:latest")


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return r.returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_ready(), reason=f"docker or image {IMAGE} not available"),
]


@pytest.fixture(autouse=True)
def docker_runtime(monkeypatch):
    monkeypatch.setenv("FOREMAN_RUNTIME", "docker")
    monkeypatch.setenv("FOREMAN_EXECUTOR_IMAGE", IMAGE)
    monkeypatch.setenv("FOREMAN_GITHUB_TOKEN", "ghp_must_not_leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")


PROBE = """
id -u > probe_uid
pwd > probe_pwd
env | sort > probe_env
cat /proc/mounts > probe_mounts
echo 'VALUE = 42' > src/app.py
"""


async def test_task_runs_in_isolated_container(client, origin, env):
    p = make_project(client, origin)
    t = make_task(client, p["id"], script=PROBE, validation="grep -q 42 src/app.py && id -u")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=120)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason

    from foreman import gitops

    repo = gitops.repo_dir(p["id"])
    assert (repo / "src" / "app.py").read_text() == "VALUE = 42\n"
    assert (repo / "probe_uid").read_text().strip() == "1000"  # never root
    assert (repo / "probe_pwd").read_text().strip() == "/workspace"
    env_dump = (repo / "probe_env").read_text()
    assert "must_not_leak" not in env_dump and "must-not-leak" not in env_dump  # NFR-5
    mounts = (repo / "probe_mounts").read_text()
    # Only the task clone and the read-only control dir come from the host.
    assert str(env) not in mounts
    assert " /workspace " in mounts and " /foreman " in mounts
    ctl_line = next(line for line in mounts.splitlines() if " /foreman " in line)
    assert " ro," in ctl_line or ctl_line.split()[3].startswith("ro")


def test_stop_kills_container_within_10s(client, origin):
    import sys

    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "foreman.cli", c],
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for c in ("supervisor", "orchestrator")
    ]
    try:
        p = make_project(client, origin)
        t = make_task(client, p["id"], script="sleep 300 & sleep 300 & wait")

        def container() -> str | None:
            names = [n for n, run in running_containers().items() if run]
            return names[0] if names else None

        wait_for(lambda: container() is not None, timeout=60)
        name = container()
        t0 = time.monotonic()
        client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "stop"})
        wait_for(lambda: name not in running_containers(), timeout=10)
        assert time.monotonic() - t0 < 10
        wait_for(lambda: task(t["id"]).state == TaskState.STOPPED_BY_USER)
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(10)


async def test_opencode_executor_against_fake_model(client, origin):
    """Real OpenCode in the executor container, driven by a scripted model server."""
    from sqlmodel import select

    from foreman.core.models import Event
    from foreman.db import session
    from tests.fakellm import MODEL, FakeLLM

    llm = FakeLLM().start()
    try:
        n = client.post(
            "/api/nodes",
            json={"name": "fake", "base_url": llm.url},
        ).json()
        p = make_project(
            client,
            origin,
            executor_kind="opencode",
            role_bindings={"executor": {"node_id": n["id"], "model": MODEL}},
        )
        t = make_task(
            client,
            p["id"],
            script="",
            title="Create hello.txt",
            validation="grep -q 'fake model' hello.txt",
            allowed_globs=["hello.txt"],
        )
        await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=180)
        with session() as s:
            events = s.exec(select(Event).where(Event.task_id == t["id"])).all()
        log = "\n".join(e.message for e in events)
        assert task(t["id"]).state == TaskState.DONE, f"{task(t['id']).reason}\n{log[-3000:]}"
        from foreman import gitops

        assert "fake model" in (gitops.repo_dir(p["id"]) / "hello.txt").read_text()
        prompts = json.dumps(llm.requests)
        assert "Create hello.txt" in prompts  # the context pack reached the model
    finally:
        llm.stop()


async def test_spec_to_done_with_opencode(client, origin):
    """E2E-1: spec chat -> approve -> decomposition -> plan approval -> OpenCode in a
    container implements the task -> validation -> merged. One fake model plays both
    the planner and the coding agent."""
    from sqlmodel import select

    from foreman import gitops
    from foreman.core.models import ProjectPhase, Task
    from foreman.db import session
    from tests.fakellm import MODEL, FakeLLM, system_text
    from tests.helpers import project

    spec_dir = "specs/001-initial"
    chat = (
        f"<<<FILE {spec_dir}/spec.md\n- FR-1 Create hello.txt mentioning the fake model.\n>>>\n"
        f"<<<FILE {spec_dir}/plan.md\nOne file.\n>>>\n"
        f"<<<FILE {spec_dir}/validation.md\n- V1 (FR-1) hello.txt exists.\n>>>\n"
        "<<<ACCEPTANCE_CMD\ngrep -q 'fake model' hello.txt\n>>>\nDone."
    )
    tasks = [
        {
            "key": "T1",
            "title": "Create hello.txt",
            "req_refs": ["FR-1"],
            "allowed_files": ["hello.txt"],
            "context_files": [],
            "deps": [],
            "validation_cmd": "grep -q 'fake model' hello.txt",
        }
    ]

    def planner(req):
        if "Break the approved plan" in system_text(req):
            return json.dumps(tasks)
        return chat

    llm = FakeLLM(planner=planner).start()
    try:
        planner_node = client.post("/api/nodes", json={"name": "p", "base_url": llm.url}).json()
        exec_node = client.post(
            "/api/nodes",
            json={"name": "e", "base_url": llm.url},
        ).json()
        p = client.post(
            "/api/projects",
            json={
                "name": "hello",
                "repo_url": str(origin),
                "executor_kind": "opencode",
                "role_bindings": {
                    "planner": {"node_id": planner_node["id"], "model": MODEL},
                    "executor": {"node_id": exec_node["id"], "model": MODEL},
                },
            },
        ).json()
        pid = p["id"]
        orch = Orchestrator()
        client.post(f"/api/projects/{pid}/spec/messages", json={"content": "hello file"})
        await drive(
            orch, lambda: bool(client.get(f"/api/projects/{pid}/spec").json()["acceptance_cmd"])
        )
        assert client.post(f"/api/projects/{pid}/spec/approve").status_code == 200
        await drive(orch, lambda: project(pid).phase == ProjectPhase.PLAN_REVIEW)
        assert client.post(f"/api/projects/{pid}/plan/approve").status_code == 200
        with session() as s:
            t = s.exec(select(Task).where(Task.project_id == pid)).one()
        await drive(orch, lambda: task(t.id).state in TERMINAL, timeout=180)
        assert task(t.id).state == TaskState.DONE, task(t.id).reason
        assert "fake model" in (gitops.repo_dir(pid) / "hello.txt").read_text()
        # The executor's prompt was the context pack, with the requirement slice.
        exec_prompts = json.dumps([r for r in llm.requests if r.get("tools")])
        assert "FR-1 Create hello.txt" in exec_prompts
    finally:
        llm.stop()


def _toolchain(nonce: str, extra: str = "") -> str:
    # The nonce makes each test run build afresh instead of reusing an earlier image.
    return (
        "FROM foreman/executor-base\n"
        f"# test {nonce}\n"
        "USER root\n"
        "RUN echo toolchain-ok > /opt/toolchain\n"
        f"{extra}"
        "USER agent\n"
    )


def _remove_project_images() -> None:
    out = subprocess.run(
        ["docker", "images", "-q", "--filter", "label=foreman=1", "foreman/project-*"],
        capture_output=True,
        text=True,
    ).stdout.split()
    if out:
        subprocess.run(["docker", "rmi", "-f", *out], capture_output=True)


async def test_tasks_run_in_the_project_toolchain_image(client, origin, tmp_path):
    from sqlmodel import select

    from foreman.core.models import Event, TaskRun
    from foreman.db import session
    from tests.test_images import add_to_origin

    add_to_origin(origin, tmp_path, {".foreman/Dockerfile": _toolchain(str(time.time_ns()))})
    p = make_project(client, origin)
    try:
        a = make_task(
            client, p["id"], "cp /opt/toolchain out.txt", validation="grep -q ok /opt/toolchain"
        )
        b = make_task(client, p["id"], "echo b > b.txt", validation="test -f /opt/toolchain")
        await drive(
            Orchestrator(),
            lambda: {task(a["id"]).state, task(b["id"]).state} <= TERMINAL,
            timeout=300,
        )
        assert task(a["id"]).state == TaskState.DONE, task(a["id"]).reason
        assert task(b["id"]).state == TaskState.DONE, task(b["id"]).reason
        with session() as s:
            builds = s.exec(
                select(Event).where(Event.kind == "log", Event.message.contains("toolchain-ok"))
            ).all()
            runs = s.exec(select(TaskRun)).all()
        # Built once (the second task reuses the image), and both runs used it.
        assert {e.task_id for e in builds} == {a["id"]}
        assert len(runs) == 2
    finally:
        _remove_project_images()


async def test_broken_toolchain_fails_as_configuration(client, origin, tmp_path):
    from sqlmodel import select

    from foreman.core.models import TaskRun
    from foreman.db import session
    from tests.test_images import add_to_origin

    broken = _toolchain(str(time.time_ns()), "RUN exit 7\n")
    add_to_origin(origin, tmp_path, {".foreman/Dockerfile": broken})
    p = make_project(client, origin)
    try:
        t = make_task(client, p["id"], "echo never > ran.txt")
        await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=300)
        assert task(t["id"]).state == TaskState.FAILED
        assert "building .foreman/Dockerfile failed" in task(t["id"]).reason
        with session() as s:
            run = s.exec(select(TaskRun)).one()
        assert run.failure_kind == "config"
    finally:
        _remove_project_images()
