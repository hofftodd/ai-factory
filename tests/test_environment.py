"""The Environment Agent: one checked toolchain image before any task runs.

Docker builds and checks are faked here (tests/test_docker.py builds a real one).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlmodel import col, select

from foreman import gitops, images
from foreman.core import roles
from foreman.core.models import Event, JobState, Project, ProjectPhase, Task, TaskState
from foreman.db import session
from foreman.orchestrator import environment
from foreman.orchestrator.environment import EnvError, parse_files
from foreman.orchestrator.main import Orchestrator
from tests.fakellm import FakeLLM, last_user_text, system_text
from tests.helpers import drive, project
from tests.test_spec_flow import approved, job_states, scripted, spec_project

ENV_ANSWER = """Here is the toolchain.

<<<FILE .foreman/Dockerfile
FROM foreman/executor-base
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pytest
USER agent
>>>
<<<FILE .foreman/check.sh
python3 --version
pytest --version
>>>
Python 3 and pytest."""


class Docker:
    """Stands in for image builds and checks."""

    def __init__(self) -> None:
        self.build_error = ""
        self.builds: list[str] = []

    async def ensure_image(self, project_id: int, workspace: Path, **_: Any) -> str:
        self.builds.append((workspace / images.DOCKERFILE).read_text())
        if self.build_error:
            raise images.ImageBuildError("building .foreman/Dockerfile failed", self.build_error)
        return f"foreman/project-test-{project_id}:abc"

    async def run_check(self, tag: str, context: Path, on_handle: Any) -> tuple[int, str]:
        return 0, (context / "check.sh").read_text()


@pytest.fixture
def docker(monkeypatch):
    fake = Docker()
    monkeypatch.setattr(environment, "docker_available", lambda: True)
    monkeypatch.setattr(images, "ensure_image", fake.ensure_image)
    monkeypatch.setattr(environment, "run_check", fake.run_check)
    return fake


@pytest.fixture
def llm():
    plan = scripted()

    def planner(req: dict[str, Any]) -> str:
        if "You are the Environment Agent" in system_text(req):
            return ENV_ANSWER
        return plan(req)

    server = FakeLLM(host="127.0.0.1", planner=planner).start()
    yield server
    server.stop()


def env_requests(llm: FakeLLM) -> list[dict[str, Any]]:
    return [r for r in llm.requests if "You are the Environment Agent" in system_text(r)]


def tasks_of(pid: int) -> list[Task]:
    with session() as s:
        return list(s.exec(select(Task).where(Task.project_id == pid).order_by(col(Task.id))))


async def test_environment_is_prepared_before_any_task_runs(client, origin, llm, docker):
    p = spec_project(client, origin, llm)
    pid = p["id"]
    orch = Orchestrator()
    await approved(client, orch, pid)
    r = client.post(f"/api/projects/{pid}/plan/approve")
    assert r.status_code == 200 and r.json()["phase"] == "environment"
    assert project(pid).phase == ProjectPhase.ENVIRONMENT

    await drive(orch, lambda: project(pid).phase == ProjectPhase.EXECUTE)
    assert project(pid).env_commit
    repo = gitops.repo_dir(pid)
    assert "python3-pytest" in (repo / ".foreman" / "Dockerfile").read_text()
    assert (repo / ".foreman" / "check.sh").read_text() == "python3 --version\npytest --version\n"
    (prompt,) = [system_text(r) for r in env_requests(llm)]
    assert "## The approved tasks" in prompt and "Write greeter" in prompt
    assert "Greeting a name gives" in prompt  # the requirements

    with session() as s:
        events = s.exec(select(Event).where(Event.project_id == pid).order_by(col(Event.id))).all()
    ready = next(e.id for e in events if e.message.startswith("environment ready"))
    started = [e.id for e in events if e.task_id is not None and e.message == "running"]
    assert all(i > ready for i in started)  # nothing ran before the image was ready


async def test_failed_builds_are_retried_with_the_error_then_wait(client, origin, llm, docker):
    docker.build_error = "E: Unable to locate package python3.99"
    p = spec_project(client, origin, llm)
    pid = p["id"]
    orch = Orchestrator()
    await approved(client, orch, pid)
    client.post(f"/api/projects/{pid}/plan/approve")
    await drive(orch, lambda: job_states(pid)[-1] == JobState.FAILED)
    reqs = env_requests(llm)
    assert len(reqs) == environment.MAX_ATTEMPTS
    assert "Unable to locate package python3.99" in last_user_text(reqs[-1])
    for _ in range(3):
        await orch.tick()
    assert project(pid).phase == ProjectPhase.ENVIRONMENT and not project(pid).env_commit
    assert all(t.state == TaskState.QUEUED for t in tasks_of(pid))

    assert client.post(f"/api/projects/{pid}/environment/skip").status_code == 200
    assert project(pid).phase == ProjectPhase.EXECUTE


async def test_rebuild_with_a_note_improves_the_current_toolchain(client, origin, llm, docker):
    p = spec_project(client, origin, llm)
    pid = p["id"]
    orch = Orchestrator()
    await approved(client, orch, pid)
    client.post(f"/api/projects/{pid}/plan/approve")
    await drive(orch, lambda: project(pid).phase == ProjectPhase.EXECUTE)

    r = client.post(f"/api/projects/{pid}/environment/rebuild", json={"note": "add jq"})
    assert r.status_code == 200
    await drive(orch, lambda: job_states(pid)[-1] == JobState.DONE and len(env_requests(llm)) == 2)
    prompt = system_text(env_requests(llm)[-1])
    assert "## Requested by the user\nadd jq" in prompt
    assert "## The current toolchain" in prompt and "python3-pytest" in prompt
    assert project(pid).phase == ProjectPhase.EXECUTE  # a rebuild doesn't stop the work


async def test_without_docker_execution_starts_directly(client, origin, llm):
    p = spec_project(client, origin, llm)
    pid = p["id"]
    await approved(client, Orchestrator(), pid)
    r = client.post(f"/api/projects/{pid}/plan/approve")
    assert r.json()["phase"] == "execute"
    r = client.post(f"/api/projects/{pid}/environment/rebuild", json={})
    assert r.status_code == 400 and "docker runtime" in r.json()["detail"]


def test_environment_needed_again_only_when_requirements_or_design_change(
    client, origin, llm, docker
):
    p = spec_project(client, origin, llm)
    pid = p["id"]
    with session() as s:
        row = s.get(Project, pid)
        assert row is not None
        assert environment.needed(row)  # never prepared
        row.env_commit = gitops.head_commit(pid)
        s.add(row)
        s.commit()
        s.refresh(row)
    assert not environment.needed(project(pid))
    gitops.write_and_commit(pid, {"specs/001-initial/acceptance-criteria.md": "x"}, "ac")
    assert not environment.needed(project(pid))
    gitops.write_and_commit(pid, {"specs/001-initial/design.md": "Now in Go."}, "design")
    assert environment.needed(project(pid))


def test_parse_files_accepts_only_the_toolchain():
    files = parse_files(ENV_ANSWER)
    assert sorted(files) == [".foreman/Dockerfile", ".foreman/check.sh"]
    with pytest.raises(EnvError, match="outside .foreman"):
        parse_files(ENV_ANSWER + "\n<<<FILE src/app.py\nprint(1)\n>>>\n")
    with pytest.raises(EnvError, match="missing .foreman/check.sh"):
        parse_files(ENV_ANSWER.split("<<<FILE .foreman/check.sh")[0])
    with pytest.raises(images.ImageBuildError, match="foreman/executor-base"):
        parse_files(ENV_ANSWER.replace("FROM foreman/executor-base", "FROM ubuntu:24.04"))


def test_tester_and_builder_may_not_touch_the_toolchain():
    p = Project(name="p", repo_url="x")
    changed = [".foreman/Dockerfile", "src/app.py", "tests/test_app.py"]
    assert roles.fence(p, roles.BUILDER, changed) == [".foreman/Dockerfile", "tests/test_app.py"]
    assert roles.fence(p, roles.TESTER, changed) == [".foreman/Dockerfile", "src/app.py"]


def test_environment_agent_is_told_how_tests_run():
    project = Project(name="p", repo_url="x", test_note="npm test (vitest)")
    prompt = environment.system_prompt(project, {}, [], {}, "")
    assert (
        "## How this project's tests run (from the user)" in prompt
        and "npm test (vitest)" in prompt
    )
    assert "## How this project's tests run" not in environment.system_prompt(
        Project(name="p", repo_url="x"), {}, [], {}, ""
    )
