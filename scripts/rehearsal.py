"""Rehearse ai-factory end to end on a small full-stack project before a large run.

The first large run (future-lab) needed 25 ai-factory PRs to get through: the bugs only
showed up on a real project, on real models. This runs examples/rehearsal (sample-desk,
a few hours of work) on a running ai-factory with your own nodes and models:

1. push examples/rehearsal into a fresh repository (a local bare repo by default, or
   --origin, an empty repository the ai-factory host can clone);
2. create a project there, with the agents' models copied from an existing project
   (--like);
3. approve the documents and the plan, then follow it to the milestone, telling you
   when a checkpoint is waiting in the inbox;
4. print the run report and say whether the rehearsal passed: the milestone reached,
   the app check passing, no failure caused by ai-factory itself.

    uv run python scripts/rehearsal.py --like 3
    uv run python scripts/rehearsal.py --like 3 --url http://factory:8700 \\
        --origin git@github.com:me/sample-desk-rehearsal.git

A local bare repository only works when ai-factory runs on this machine.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "rehearsal"
POLL_S = 60.0


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def prepare_origin(work: Path, origin: str | None = None, example: Path = EXAMPLE) -> str:
    """Push the example project into `origin` (default: a new bare repo under `work`)."""
    if origin is None:
        bare = work / "origin.git"
        _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=work)
        origin = str(bare)
    seed = work / "seed"
    shutil.copytree(example, seed)
    _git("init", "-q", "-b", "main", cwd=seed)
    _git("add", "-A", cwd=seed)
    _git(
        "-c", "user.name=ai-factory rehearsal", "-c", "user.email=rehearsal@ai-factory.local",
        "commit", "-qm", "sample-desk: approved planning documents", cwd=seed,
    )  # fmt: skip
    _git("push", "-q", origin, "main", cwd=seed)
    return origin


def create_project(client: httpx.Client, like: int, repo_url: str, name: str) -> dict[str, Any]:
    """A project on `repo_url` whose agents use the same models as project `like`."""
    r = client.get(f"/api/projects/{like}")
    r.raise_for_status()
    model = r.json()
    bindings = {
        role: {k: b[k] for k in ("node_id", "model", "reasoning_effort", "also") if k in b}
        for role, b in (model.get("role_bindings") or {}).items()
        if isinstance(b, dict) and b.get("node_id") is not None
    }
    body = {
        "name": name,
        "repo_url": repo_url,
        "executor_kind": model.get("executor_kind", "pi"),
        "network": model.get("network", "isolated"),
        "role_bindings": bindings,
        "max_parallel_tasks": model.get("max_parallel_tasks", 1),
        "min_task_coverage": model.get("min_task_coverage", 80),
        # The run should need you only where a person must decide.
        "gates": {"plan_approval": True, "milestone": True, "review": True},
    }
    r = client.post("/api/projects", json=body)
    r.raise_for_status()
    return dict(r.json())


def step(client: httpx.Client, pid: int, said: set[int], log: Callable[[str], None]) -> bool:
    """Move the project along one notch. True once the milestone is reached."""
    project = client.get(f"/api/projects/{pid}").json()
    phase = project["phase"]
    if phase == "specify":
        client.post(f"/api/projects/{pid}/spec/approve").raise_for_status()
        log("documents approved; the Task Planner is breaking down the work")
    elif phase == "plan_review":
        client.post(f"/api/projects/{pid}/plan/approve").raise_for_status()
        log("plan approved; the Environment Agent prepares the toolchain")
    elif phase == "done":
        return True
    for gate in client.get("/api/gates", params={"project_id": pid}).json():
        if gate["kind"] == "milestone":
            return True
        if gate["id"] not in said:
            said.add(gate["id"])
            log(f"waiting for you in the inbox: {gate['title']}")
    return False


def verdict(client: httpx.Client, pid: int, report: str, reached: bool) -> list[str]:
    """What keeps the rehearsal from passing (empty: it passed)."""
    problems = [] if reached else ["the milestone was not reached in time"]
    if "\n| factory: " in report:
        problems.append("attempts failed because of ai-factory itself (see the report)")
    app = client.get(f"/api/projects/{pid}/tests").json().get("app")
    if app is None:
        problems.append("the app check never ran: no scripts/start.sh")
    elif app["failed"] + app["errors"]:
        problems.append("the last app check found problems with the running app")
    objections = [
        g for g in client.get("/api/gates", params={"project_id": pid, "state": "all"}).json()
        if g["kind"] == "review"
    ]  # fmt: skip
    if objections:
        n = len(objections)
        problems.append(f"the Reviewer still objected after its rounds on {n} task(s)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--like", type=int, required=True, help="copy agents' models from")
    parser.add_argument("--url", default="http://127.0.0.1:8700", help="the ai-factory API")
    parser.add_argument("--origin", help="an empty repository to push the project into")
    parser.add_argument("--hours", type=float, default=8.0, help="give up after this long")
    args = parser.parse_args(argv)

    def log(msg: str) -> None:
        print(time.strftime("%H:%M ") + msg, flush=True)

    work = Path(tempfile.mkdtemp(prefix="ai-factory-rehearsal-"))
    with httpx.Client(base_url=args.url, timeout=60) as client:
        repo = prepare_origin(work, args.origin)
        name = time.strftime("rehearsal-%Y%m%d-%H%M")
        pid = int(create_project(client, args.like, repo, name)["id"])
        log(f"project {pid} ({name}) on {repo}: {args.url}/projects/{pid}")
        deadline, said, reached = time.time() + args.hours * 3600, set[int](), False
        while time.time() < deadline:
            if step(client, pid, said, log):
                reached = True
                break
            time.sleep(POLL_S)
        report = client.get(f"/api/projects/{pid}/report").text
        print(report)
        problems = verdict(client, pid, report, reached)
    for p in problems:
        log(f"FAIL: {p}")
    if not problems:
        log("rehearsal passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
