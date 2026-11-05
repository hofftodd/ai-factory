"""Stop controls end to end, with orchestrator and supervisor as real OS processes.

These are the highest-priority checks (validation.md V8.x, V9.1).
"""

from __future__ import annotations

import http.server
import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from sqlmodel import col, select

from foreman.core.models import RunState, TaskRun, TaskState
from foreman.db import session
from foreman.sandbox import alive_in_group
from tests.helpers import make_project, make_task, task, wait_for

LONG = "sleep 300 & sleep 300 & wait"


def _start(component: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "foreman.cli", component],
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def procs():
    started: dict[str, subprocess.Popen] = {}

    def start(name: str) -> subprocess.Popen:
        started[name] = _start(name)
        return started[name]

    start("supervisor")
    start("orchestrator")
    yield started, start
    for p in started.values():
        if p.poll() is None:
            with _suppress():
                os.kill(p.pid, signal.SIGCONT)
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _alive_in_group(pgid: int) -> list[int]:
    """Non-zombie processes in a process group."""
    return alive_in_group(pgid)


def _open_run(task_id: int) -> TaskRun:
    holder: list[TaskRun] = []

    def has_handle() -> bool:
        with session() as s:
            run = s.exec(
                select(TaskRun).where(TaskRun.task_id == task_id).order_by(col(TaskRun.id).desc())
            ).first()
        if run and run.handle and run.state == RunState.RUNNING:
            holder.append(run)
            return True
        return False

    wait_for(has_handle, timeout=20)
    return holder[-1]


def _pgid(run: TaskRun) -> int:
    kind, _, ident = run.handle.partition(":")
    assert kind == "local"
    pgid = int(ident)
    wait_for(lambda: len(_alive_in_group(pgid)) >= 3, timeout=10)  # bash + 2 sleeps
    return pgid


def _run_state(run_id: int) -> RunState:
    with session() as s:
        return s.get(TaskRun, run_id).state


def test_stop_task_kills_process_tree_within_10s(client, origin, procs):
    """V8.1"""
    p = make_project(client, origin)
    t = make_task(client, p["id"], script=f"echo partial > wip.txt; {LONG}")
    run = _open_run(t["id"])
    pgid = _pgid(run)

    t0 = time.monotonic()
    client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "stop"})
    wait_for(lambda: not _alive_in_group(pgid), timeout=10)
    assert time.monotonic() - t0 < 10

    wait_for(lambda: task(t["id"]).state == TaskState.STOPPED_BY_USER)
    assert _run_state(run.id) == RunState.STOPPED
    # Work is preserved for inspection / retry.
    from foreman import gitops

    assert (gitops.task_dir(p["id"], t["id"]) / "wip.txt").read_text() == "partial\n"
    diff = client.get(f"/api/tasks/{t['id']}/diff").json()
    assert diff["files"] == ["wip.txt"]


def test_stop_works_while_orchestrator_is_frozen(client, origin, procs):
    """V8.2: the supervisor enforces Stop even if the orchestrator is hung."""
    started, _ = procs
    p = make_project(client, origin)
    t = make_task(client, p["id"], script=LONG)
    run = _open_run(t["id"])
    pgid = _pgid(run)

    os.kill(started["orchestrator"].pid, signal.SIGSTOP)
    try:
        t0 = time.monotonic()
        client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "stop"})
        wait_for(lambda: not _alive_in_group(pgid), timeout=10)
        assert time.monotonic() - t0 < 10
        # The dashboard can already tell a stop is under way (read from control.db).
        # Either the record already landed or the dashboard shows the stop as pending.
        info = client.get(f"/api/tasks/{t['id']}").json()
        assert info["stop_pending"] or info["state"] == "stopped_by_user"
    finally:
        os.kill(started["orchestrator"].pid, signal.SIGCONT)
    # The bookkeeping lands once the lock frees, and the orchestrator must not resurrect,
    # advance or fail the task when it wakes up.
    wait_for(lambda: task(t["id"]).state == TaskState.STOPPED_BY_USER, timeout=40)
    time.sleep(1.5)
    assert task(t["id"]).state == TaskState.STOPPED_BY_USER
    assert _run_state(run.id) == RunState.STOPPED


def test_estop_halts_everything_until_cleared(client, origin, procs):
    """V8.3"""
    p1 = make_project(client, origin, name="one")
    p2 = make_project(client, origin, name="two")
    t1 = make_task(client, p1["id"], script=LONG)
    t2 = make_task(client, p2["id"], script=LONG)
    g1, g2 = _pgid(_open_run(t1["id"])), _pgid(_open_run(t2["id"]))

    client.post("/api/controls", json={"scope": "global", "action": "estop"})
    wait_for(lambda: not _alive_in_group(g1) and not _alive_in_group(g2), timeout=10)
    # Kill first, bookkeeping second: the state records follow right after.
    wait_for(lambda: task(t1["id"]).state == TaskState.STOPPED_BY_USER, timeout=5)
    wait_for(lambda: task(t2["id"]).state == TaskState.STOPPED_BY_USER, timeout=5)

    p3 = make_project(client, origin, name="three")
    t3 = make_task(client, p3["id"], script="true")
    time.sleep(1.5)
    assert task(t3["id"]).state == TaskState.QUEUED  # nothing starts during E-stop

    client.post("/api/controls", json={"scope": "global", "action": "clear_estop"})
    wait_for(lambda: task(t3["id"]).state == TaskState.DONE, timeout=20)


def test_stop_project_pauses_scheduling(client, origin, procs):
    p = make_project(client, origin)
    a = make_task(client, p["id"], script=LONG)
    b = make_task(client, p["id"], script="true")
    g = _pgid(_open_run(a["id"]))
    client.post("/api/controls", json={"scope": "project", "target_id": p["id"], "action": "stop"})
    wait_for(lambda: not _alive_in_group(g), timeout=10)
    time.sleep(1.5)
    assert task(a["id"]).state == TaskState.STOPPED_BY_USER
    assert task(b["id"]).state == TaskState.QUEUED
    client.post(
        "/api/controls", json={"scope": "project", "target_id": p["id"], "action": "resume"}
    )
    wait_for(lambda: task(b["id"]).state == TaskState.DONE, timeout=20)


def test_late_stop_bookkeeping_does_not_undo_resume(client, origin):
    """A Stop the supervisor finishes after the user pressed Resume must not re-pause."""
    from concurrent.futures import ThreadPoolExecutor

    from foreman.core.models import Control, ControlAction, ControlScope
    from foreman.supervisor.main import apply_control
    from tests.helpers import project

    p = make_project(client, origin)
    stale = Control(
        scope=ControlScope.PROJECT,
        target_id=p["id"],
        action=ControlAction.STOP,
        created_at=time.time() - 5,
    )
    client.post(
        "/api/controls", json={"scope": "project", "target_id": p["id"], "action": "resume"}
    )
    with ThreadPoolExecutor(1) as pool:
        apply_control(stale, pool)
    assert not project(p["id"]).paused


def test_orchestrator_crash_recovery(client, origin, procs):
    """V9.1: kill -9 mid-run; on restart the run is closed and its processes killed."""
    started, start = procs
    p = make_project(client, origin)
    t = make_task(client, p["id"], script=LONG)
    run = _open_run(t["id"])
    pgid = _pgid(run)

    started["orchestrator"].kill()
    started["orchestrator"].wait()
    assert _alive_in_group(pgid)  # the agent outlived its orchestrator...
    start("orchestrator")
    wait_for(lambda: not _alive_in_group(pgid), timeout=15)  # ...until recovery
    wait_for(lambda: task(t["id"]).state == TaskState.FAILED)
    assert "restarted" in task(t["id"]).reason
    assert _run_state(run.id) == RunState.INTERRUPTED


class _StreamHandler(http.server.BaseHTTPRequestHandler):
    disconnected = threading.Event()

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"data: token\n\n")
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            type(self).disconnected.set()

    def log_message(self, *args):
        pass


def test_stop_aborts_inflight_model_stream(client, origin, procs):
    """V8.5: killing the executor closes its streaming connection to the model server."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat"
    reader = f"import urllib.request; r = urllib.request.urlopen('{url}')\nwhile r.read(16): pass"
    try:
        p = make_project(client, origin)
        script = f'{sys.executable} -c "{reader}"'
        t = make_task(client, p["id"], script=script)
        _open_run(t["id"])
        time.sleep(1)
        assert not _StreamHandler.disconnected.is_set()
        client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "stop"})
        assert _StreamHandler.disconnected.wait(10)
    finally:
        server.shutdown()
