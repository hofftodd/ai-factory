"""scripts/rehearsal.py: the parts that drive ai-factory's API (a full rehearsal needs
real models and hours, so it isn't run here)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from ai_factory.core import gates
from ai_factory.core.models import GateKind

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rehearsal.py"
_spec = importlib.util.spec_from_file_location("rehearsal", SCRIPT)
assert _spec is not None and _spec.loader is not None
rehearsal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rehearsal)


@pytest.fixture
def template(client):
    node = client.post("/api/nodes", json={"name": "big", "base_url": "http://x:1"}).json()
    body = {
        "name": "template",
        "repo_url": str(rehearsal.prepare_origin(_tmp(client), None)),
        "executor_kind": "pi",
        "role_bindings": {
            "tester": {"node_id": node["id"], "model": "coder", "reasoning_effort": "high"},
            "builder": {"node_id": node["id"], "model": "coder"},
            "task_planner": {"node_id": node["id"], "model": "planner"},
        },
        "max_parallel_tasks": 2,
    }
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


_n = [0]


def _tmp(client) -> Path:
    from ai_factory.config import get_settings

    _n[0] += 1
    path = get_settings().data_dir / f"rehearsal-{_n[0]}"
    path.mkdir(parents=True)
    return path


def test_the_example_is_pushed_with_its_approved_documents(tmp_path):
    origin = rehearsal.prepare_origin(tmp_path)
    files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "main"],
        cwd=origin, capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    for doc in ("requirements.md", "design.md", "acceptance-criteria.md"):
        assert f"specs/001-initial/{doc}" in files


def test_the_example_asks_for_users_workflows_and_demo_data():
    requirements = (rehearsal.EXAMPLE / "specs/001-initial/requirements.md").read_text()
    assert "## Users and workflows" in requirements and "## Demo data" in requirements


def test_a_rehearsal_project_uses_the_template_models(client, template):
    repo = rehearsal.prepare_origin(_tmp(client), None)
    p = rehearsal.create_project(client, template["id"], repo, "rehearsal-x")
    assert p["phase"] == "specify" and p["max_parallel_tasks"] == 2
    assert p["role_bindings"]["tester"]["model"] == "coder"
    assert p["role_bindings"]["tester"]["reasoning_effort"] == "high"
    assert p["gates"] == {"plan_approval": True, "milestone": True, "review": True}


def test_a_step_approves_the_documents_and_reports_waiting_checkpoints(client, template):
    repo = rehearsal.prepare_origin(_tmp(client), None)
    pid = rehearsal.create_project(client, template["id"], repo, "rehearsal-y")["id"]
    said: set[int] = set()
    log: list[str] = []
    assert rehearsal.step(client, pid, said, log.append) is False
    assert client.get(f"/api/projects/{pid}").json()["phase"] == "decomposing"
    gates.open_gate(pid, GateKind.SPEC_CHANGE, "Change the design?")
    rehearsal.step(client, pid, said, log.append)
    rehearsal.step(client, pid, said, log.append)  # said once, not every poll
    assert log.count("waiting for you in the inbox: Change the design?") == 1
    gates.open_gate(pid, GateKind.MILESTONE, "done")
    assert rehearsal.step(client, pid, said, log.append) is True


def test_the_verdict_names_what_kept_it_from_passing(client, template):
    pid = template["id"]
    report = "# Run report\n| factory: x | 1 | internal 1 |"
    problems = rehearsal.verdict(client, pid, report, False)
    assert problems == [
        "the milestone was not reached in time",
        "attempts failed because of ai-factory itself (see the report)",
        "the app check never ran: no scripts/start.sh",
    ]
