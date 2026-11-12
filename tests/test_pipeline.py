"""The Tester -> Builder pipeline: role fences, test results, coverage (local runtime).

Scripted stand-ins play the agents: the Tester's script writes a tiny test runner
(tests/run.sh: every tests/check_*.sh is a test; it writes JUnit XML and LCOV like a
real runner would) and a test; the Builder's script writes the code.
"""

from __future__ import annotations

import json
import shutil

import pytest
from sqlmodel import col, select

from ai_factory import gitops
from ai_factory.config import get_settings
from ai_factory.core.models import (
    Event,
    Gate,
    GateKind,
    GateState,
    Task,
    TaskRun,
    TaskState,
    TestCase,
    TestRun,
)
from ai_factory.db import session
from ai_factory.orchestrator.main import Orchestrator
from tests.conftest import git
from tests.helpers import TERMINAL, drive, make_project, task

RUN_SH = r"""out="${AI_FACTORY_RESULTS:-/tmp}"
pass=0; fail=0; cases=""
for t in tests/check_*.sh; do
  name=$(basename "$t" .sh)
  if sh "$t" >/dev/null 2>&1; then
    cases="$cases<testcase classname=\"tests.$name\" name=\"$name\" file=\"$t\" time=\"0.01\"/>"
    pass=$((pass+1))
  else
    f="<failure message=\"$name failed\"/>"
    cases="$cases<testcase classname=\"tests.$name\" name=\"$name\" file=\"$t\">$f</testcase>"
    fail=$((fail+1))
  fi
done
echo "<testsuite tests=\"$((pass+fail))\">$cases</testsuite>" > "$out/junit.xml"
# Coverage: each line of greet.sh is covered, except lines marked "# uncovered".
if [ -f greet.sh ]; then
  { echo "SF:$PWD/greet.sh"; i=0
    while IFS= read -r line; do i=$((i+1))
      case "$line" in *"# uncovered"*) echo "DA:$i,0";; *) echo "DA:$i,3";; esac
    done < greet.sh; echo end_of_record; } > "$out/lcov.info"
fi
[ "$fail" -eq 0 ]
"""

RUNNER = "mkdir -p tests\ncat > tests/run.sh <<'EOS'\n" + RUN_SH + "EOS\n"

TESTER = (
    RUNNER
    + """cat > tests/check_greet.sh <<'EOS'
test "$(sh greet.sh bob)" = "hello bob"
EOS
"""
)
BUILDER = """printf 'echo "hello $1"\\n' > greet.sh\n"""


def scripts(tester: str = TESTER, builder: str = BUILDER) -> str:
    return json.dumps({"tester": tester, "builder": builder})


def add_task(client, pid: int, executor_script: str, **kw) -> dict:
    """An agent-style task (criteria, no test command) run by role scripts."""
    from ai_factory.core.models import Project
    from ai_factory.db import session as db

    with db() as s:
        p = s.get(Project, pid)
        assert p is not None
        t = Task(
            project_id=pid,
            title=kw.pop("title", "Greet people"),
            description="Greet a person by name.",
            criteria=["`greet.sh bob` prints hello bob"],
            executor_script=executor_script,
            **kw,
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.model_dump()


def runs_of(task_id: int) -> list[TaskRun]:
    with session() as s:
        return list(
            s.exec(select(TaskRun).where(TaskRun.task_id == task_id).order_by(col(TaskRun.id)))
        )


def test_runs_of(task_id: int) -> list[TestRun]:
    with session() as s:
        return list(
            s.exec(select(TestRun).where(TestRun.task_id == task_id).order_by(col(TestRun.id)))
        )


test_runs_of.__test__ = False  # type: ignore[attr-defined]


def events(task_id: int, kind: str) -> list[Event]:
    with session() as s:
        return list(s.exec(select(Event).where(Event.task_id == task_id, Event.kind == kind)))


async def test_tester_then_builder_red_to_green(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts())
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason

    assert [r.step for r in runs_of(t["id"])] == ["tester", "builder"]
    red, green = test_runs_of(t["id"])
    assert (red.step, red.failed, red.passed) == ("tester", 1, 0)  # written to fail first
    assert (green.step, green.failed, green.passed) == ("builder", 0, 1)
    assert green.coverage == 100.0 and green.changed_coverage == 100.0
    with session() as s:
        [case] = s.exec(select(TestCase).where(TestCase.test_run_id == green.id)).all()
    assert case.task_test and case.file == "tests/check_greet.sh"
    repo = gitops.repo_dir(p["id"])
    assert (repo / "greet.sh").exists() and (repo / "tests/check_greet.sh").exists()

    out = client.get(f"/api/tasks/{t['id']}/tests").json()
    assert [r["step"] for r in out["runs"]] == ["builder", "tester"]
    assert out["runs"][0]["cases"][0]["status"] == "passed"
    suite = client.get(f"/api/projects/{p['id']}/tests").json()["suite"]
    assert suite["passed"] == 1 and suite["coverage"] == 100.0
    [point] = client.get(f"/api/projects/{p['id']}/coverage-trend").json()
    assert point["coverage"] == 100.0 and point["tests"] == 1


async def test_tester_may_not_change_code(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(tester=TESTER + BUILDER))  # also writes greet.sh
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    row = task(t["id"])
    assert row.state == TaskState.FAILED and "may not touch: greet.sh" in row.reason
    assert runs_of(t["id"])[-1].failure_kind == "fence"
    [denied] = events(t["id"], "denied")
    assert denied.message == "Tester Agent may not change non-test files"
    # Its changes were thrown away, not kept for the next attempt.
    assert not (gitops.task_dir(p["id"], t["id"]) / "greet.sh").exists()


async def test_builder_may_not_change_tests(client, origin):
    p = make_project(client, origin)
    cheat = BUILDER + "echo true > tests/check_greet.sh\n"
    t = add_task(client, p["id"], scripts(builder=cheat))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    row = task(t["id"])
    assert row.state == TaskState.FAILED and "tests/check_greet.sh" in row.reason
    assert events(t["id"], "denied")[0].message == "Builder Agent may not change tests"
    # The Tester's step stands: a retry goes straight to the Builder.
    assert row.tests_ready


async def test_builder_may_not_change_the_test_configuration(client, origin):
    p = make_project(client, origin)
    skip = BUILDER + "echo \"collect_ignore = ['tests']\" > conftest.py\n"
    t = add_task(client, p["id"], scripts(builder=skip))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    row = task(t["id"])
    assert row.state == TaskState.FAILED and "may not touch: conftest.py" in row.reason
    [denied] = events(t["id"], "denied")
    assert denied.message == "Builder Agent may not change the test configuration"


async def test_setup_may_write_the_test_configuration(client, origin):
    p = make_project(client, origin)
    config = BUILDER + "printf '[pytest]\\naddopts = -q\\n' > pytest.ini\n"
    t = add_task(client, p["id"], scripts(builder=config), kind="setup")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason


async def test_every_task_needs_tests(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(tester="true"))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert "the Tester added no tests" in task(t["id"]).reason


# A Builder that also writes scripts/start.sh: a page served on $APP_PORT.
APP = """mkdir -p scripts www
printf '#!/bin/sh\\nexec python3 -m http.server "$APP_PORT" --bind 127.0.0.1 --directory www\\n' \\
  > scripts/start.sh
echo '<!doctype html><html><body><h1>Greetings</h1></body></html>' > www/index.html
"""
# Its start script fails straight away.
BROKEN_APP = """mkdir -p scripts
printf '#!/bin/sh\\necho "cannot find module server"\\nexit 1\\n' > scripts/start.sh
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="the app check needs node")
async def test_a_working_app_passes_the_app_check(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(builder=BUILDER + APP))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason
    app_runs = [r for r in test_runs_of(t["id"]) if r.step == "app"]
    assert len(app_runs) == 1 and app_runs[0].passed >= 1 and app_runs[0].failed == 0
    assert any("app check: the app started" in e.message for e in events(t["id"], "test"))


async def test_an_app_that_wont_start_goes_back_to_the_builder(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(builder=BUILDER + BROKEN_APP))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    row = task(t["id"])
    assert row.state == TaskState.FAILED
    assert "the app check found problems" in row.reason
    assert "exited before the app answered" in row.reason
    assert runs_of(t["id"])[-1].failure_kind == "app"
    app_runs = [r for r in test_runs_of(t["id"]) if r.step == "app"]
    assert len(app_runs) == 1 and app_runs[0].failed == 1


async def test_a_project_without_an_app_skips_the_app_check(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts())
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE
    assert not [r for r in test_runs_of(t["id"]) if r.step == "app"]


async def test_failing_tests_fail_the_builder(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(builder="echo 'echo bye' > greet.sh"))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    row = task(t["id"])
    assert row.state == TaskState.FAILED and "tests fail: 1 of 1 failed" in row.reason


# Works, but one of its three lines is never run by the tests (66% < 80%).
LOW = """printf 'echo "hello $1"\\nexit 0\\necho never # uncovered\\n' > greet.sh\n"""
# Each Tester pass adds a test, so the send-back round has something new.
MORE_TESTS = (
    TESTER
    + """n=$(ls tests/check_*.sh | wc -l)
echo true > "tests/check_more_$n.sh"
"""
)


async def test_low_coverage_goes_back_to_the_tester_then_asks(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(tester=MORE_TESTS, builder=LOW))
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state == TaskState.AWAITING_MERGE, timeout=60)
    steps = [r.step for r in runs_of(t["id"])]
    assert steps == ["tester", "builder", "tester", "builder"]  # one round back
    assert test_runs_of(t["id"])[-1].changed_coverage is not None
    with session() as s:
        [gate] = s.exec(select(Gate).where(Gate.kind == GateKind.COVERAGE)).all()
    assert "minimum 80%" in gate.detail
    client.post(f"/api/gates/{gate.id}/resolve", json={"approve": True})  # merge anyway
    await drive(orch, lambda: task(t["id"]).state == TaskState.DONE)


def pack_of(run: TaskRun) -> str:
    return (get_settings().runs_dir / str(run.id) / "context_pack.md").read_text()


async def test_the_tester_hears_why_it_was_sent_back(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(tester=MORE_TESTS, builder=LOW))
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state == TaskState.AWAITING_MERGE, timeout=60)
    first, _, again, _ = runs_of(t["id"])
    assert "Sent back to you" not in pack_of(first)
    assert "only 67% of the lines this task changed (minimum 80%)" in pack_of(again)
    assert "`greet.sh`: 2 of 3 lines run by the tests" in pack_of(again)
    with session() as s:
        gate = s.exec(select(Gate).where(Gate.kind == GateKind.COVERAGE)).one()
    note = "test what happens after exit"
    client.post(f"/api/gates/{gate.id}/resolve", json={"approve": False, "note": note})
    await drive(
        orch,
        lambda: len(runs_of(t["id"])) == 6 and task(t["id"]).state == TaskState.AWAITING_MERGE,
        timeout=60,
    )
    assert note in pack_of(runs_of(t["id"])[4])


async def test_a_failed_task_can_go_back_to_the_tester_with_a_note(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(tester=MORE_TESTS, builder="echo 'echo bye' > greet.sh"))
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.FAILED
    note = "the greeting is wrong"
    r = client.post(f"/api/tasks/{t['id']}/send-back", json={"note": note})
    assert r.status_code == 200 and r.json()["state"] == "queued" and not r.json()["tests_ready"]
    await drive(orch, lambda: len(runs_of(t["id"])) == 4 and task(t["id"]).state in TERMINAL)
    assert [r.step for r in runs_of(t["id"])] == ["tester", "builder", "tester", "builder"]
    assert f"The user's note:\n{note}" in pack_of(runs_of(t["id"])[2])
    assert client.post(f"/api/tasks/{t['id']}/send-back", json={}).status_code == 200
    done = add_task(client, p["id"], scripts())
    await drive(orch, lambda: task(done["id"]).state in TERMINAL)
    assert client.post(f"/api/tasks/{done['id']}/send-back", json={}).status_code == 409


# The Builder finds tests that can't all pass, and reports instead of working around them
# (its code satisfies one side, so the suite fails).
CONFLICT = "echo 'echo bye' > greet.sh\n" + (
    """echo 'check_greet contradicts check_old' > "$AI_FACTORY_RESULTS/tests-conflict.md"\n"""
)
# A Builder that gets everything green and still writes the file, to say there's no conflict.
NO_CONFLICT_NOTE = BUILDER + (
    """echo 'No conflicting tests found' > "$AI_FACTORY_RESULTS/tests-conflict.md"\n"""
)


async def test_the_builder_reports_contradicting_tests_once(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(tester=MORE_TESTS, builder=CONFLICT))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=60)
    runs = runs_of(t["id"])
    assert [r.step for r in runs] == ["tester", "builder", "tester", "builder"]
    assert runs[1].failure_kind == "tests_conflict"
    assert "check_greet contradicts check_old" in pack_of(runs[2])
    assert "contradict each other" in pack_of(runs[2])
    # A second report in a row fails the attempt instead of another round.
    row = task(t["id"])
    assert row.state == TaskState.FAILED and "the tests contradict each other" in row.reason


async def test_a_conflict_report_with_a_green_suite_is_ignored(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(tester=MORE_TESTS, builder=NO_CONFLICT_NOTE))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=60)
    assert task(t["id"]).state == TaskState.DONE
    assert [r.step for r in runs_of(t["id"])] == ["tester", "builder"]
    with session() as s:
        texts = [e.message for e in s.exec(select(Event).where(Event.task_id == t["id"]))]
    assert any("test-conflict report, but the whole suite passes" in m for m in texts)
    assert not any(m.startswith("sent back to the Tester") for m in texts)


def _check(name: str, body: str) -> str:
    return RUNNER + f"cat > tests/check_{name}.sh <<'EOS'\n{body}\nEOS\n"


async def test_parallel_tasks_that_break_each_other_are_caught_before_merging(client, origin):
    """A and B each pass on their own; together B's test fails. B finishes after A has
    merged, so it is re-tested with A's work and refused instead of breaking the branch."""
    p = make_project(client, origin, max_parallel_tasks=2)
    a = add_task(
        client,
        p["id"],
        scripts(tester=_check("a", "test -f a.txt"), builder="echo a > a.txt"),
        title="A",
    )
    b = add_task(
        client,
        p["id"],
        scripts(
            tester=_check("b", "test -f b.txt && test ! -f a.txt"),
            builder="sleep 3\necho b > b.txt",
        ),
        title="B",
    )
    orch = Orchestrator()
    await drive(orch, lambda: {task(a["id"]).state, task(b["id"]).state} <= TERMINAL, timeout=90)
    assert task(a["id"]).state == TaskState.DONE
    row = task(b["id"])
    assert row.state == TaskState.FAILED
    assert "tests fail after bringing in work merged" in row.reason
    # They ran side by side: B started before A was done.
    a_runs, b_runs = runs_of(a["id"]), runs_of(b["id"])
    assert b_runs[0].started_at < max(r.ended_at or 0 for r in a_runs)
    repo = gitops.repo_dir(p["id"])
    assert (repo / "a.txt").exists() and not (repo / "b.txt").exists()


def _same_file_tasks(client, origin, b_builder: str, b_tester: str = ""):
    p = make_project(client, origin, max_parallel_tasks=2)
    a = add_task(
        client,
        p["id"],
        scripts(tester=_check("a", "grep -q A shared.txt"), builder="echo A > shared.txt"),
        title="A",
    )
    b = add_task(
        client,
        p["id"],
        scripts(tester=b_tester or _check("b", "grep -q B shared.txt"), builder=b_builder),
        title="B",
    )
    return p, a, b


async def test_parallel_tasks_editing_the_same_file_go_back_to_the_builder(client, origin):
    """B conflicts with A, which merged while B ran: B's next attempt merges A's work with
    the conflicts marked, and its Builder resolves them instead of B starting over."""
    resolve = "sleep 3\nprintf 'A\\nB\\n' > shared.txt"
    p, a, b = _same_file_tasks(client, origin, resolve)
    orch = Orchestrator()
    await drive(orch, lambda: {task(a["id"]).state, task(b["id"]).state} <= TERMINAL, timeout=90)
    assert task(a["id"]).state == TaskState.DONE
    assert task(b["id"]).state == TaskState.DONE, task(b["id"]).reason
    assert [r.failure_kind for r in runs_of(b["id"]) if r.failure_kind] == ["behind"]
    assert [r.step for r in runs_of(b["id"])][-1] == "builder"  # no new Tester run
    texts = [e.message for e in events(b["id"], "system")]
    assert any("which the Builder resolves" in m for m in texts)
    assert any("the Builder resolved the merge conflicts" in m for m in texts)
    assert (gitops.repo_dir(p["id"]) / "shared.txt").read_text() == "A\nB\n"


async def test_a_conflict_found_before_the_tester_goes_to_the_builder_after_it(client, origin):
    """B's Builder sends it back to the Tester (as a coverage round does), and A merges a
    conflicting change meanwhile. B's next attempt starts with the Tester, which may not
    edit code: the merge waits until after the Tester, and the Builder resolves it."""
    builder = (
        "sleep 3\n"
        "if grep -q '^<<<<<<<' shared.txt 2>/dev/null; then printf 'A\\nB\\n' > shared.txt\n"
        "else echo X > shared.txt; echo 'check_b contradicts' > "
        '"$AI_FACTORY_RESULTS/tests-conflict.md"; fi'
    )
    # Each Tester run adds a new check, as a Tester sent back for more tests does.
    tester = _check("b_$$", "grep -q B shared.txt")
    p, a, b = _same_file_tasks(client, origin, builder, tester)
    orch = Orchestrator()
    await drive(orch, lambda: {task(a["id"]).state, task(b["id"]).state} <= TERMINAL, timeout=120)
    assert task(a["id"]).state == TaskState.DONE
    assert task(b["id"]).state == TaskState.DONE, task(b["id"]).reason
    texts = [e.message for e in events(b["id"], "system")]
    assert any("it comes in after the Tester, for the Builder to resolve" in m for m in texts)
    assert any("which the Builder resolves" in m for m in texts)
    assert any("the Builder resolved the merge conflicts" in m for m in texts)
    assert [r.step for r in runs_of(b["id"])][-2:] == ["tester", "builder"]
    assert (gitops.repo_dir(p["id"]) / "shared.txt").read_text() == "A\nB\n"


async def test_a_builder_that_leaves_conflict_markers_asks_the_user(client, origin):
    # The Builder only appends, so the markers stay.
    p, a, b = _same_file_tasks(client, origin, "sleep 3\necho B >> shared.txt")
    client.patch(f"/api/projects/{p['id']}", json={"watchdog": {"escalation": True}})
    orch = Orchestrator()
    await drive(orch, lambda: task(b["id"]).state == TaskState.BLOCKED, timeout=90)
    last = runs_of(b["id"])[-1]
    assert last.failure_kind == "conflict"
    assert "left conflict markers in shared.txt" in last.reason
    assert (gitops.repo_dir(p["id"]) / "shared.txt").read_text() == "A\n"


async def test_bytecode_the_project_tracks_trips_no_role_check(client, origin, tmp_path):
    """future-lab's branch tracked __pycache__/*.pyc. Running code rewrites them, and a
    task clone stops tracking them; neither is the Tester's (or anyone's) change."""
    seed = tmp_path / "pyc-seed"
    git("clone", "-q", str(origin), str(seed), cwd=tmp_path)
    (seed / "src" / "__pycache__").mkdir()
    (seed / "src" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00old")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q", "-m", "pyc", cwd=seed)
    git("push", "-q", "origin", "HEAD:main", cwd=seed)
    p = make_project(client, origin)
    touch = "printf '\\001new' > src/__pycache__/app.cpython-312.pyc\n"  # as python would
    t = add_task(client, p["id"], scripts(tester=TESTER + touch, builder=BUILDER + touch))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=60)
    row = task(t["id"])
    assert row.state == TaskState.DONE, row.reason
    repo = gitops.repo_dir(p["id"])
    assert not [f for f in git("ls-files", cwd=repo).split() if "__pycache__" in f]


# A Builder that must never run: it leaves a mark if it does.
NO_BUILDER = "echo built > builder-ran.txt\n"


async def test_a_task_that_changes_only_acceptance_tests_merges_without_a_builder(client, origin):
    p = make_project(client, origin)
    first = add_task(client, p["id"], scripts(), title="Greet")
    acc = (
        "mkdir -p tests/acceptance\n"
        "echo 'sh greet.sh bob | grep -q bob' > tests/acceptance/check_samples.sh\n"
    )
    t = add_task(
        client,
        p["id"],
        scripts(tester=acc, builder=NO_BUILDER),
        title="Use samples in the acceptance tests",
        deps=[first["id"]],
    )
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=60)
    row = task(t["id"])
    assert row.state == TaskState.DONE, row.reason
    assert row.no_build and [r.step for r in runs_of(t["id"])] == ["tester"]
    repo = gitops.repo_dir(p["id"])
    assert (repo / "tests" / "acceptance" / "check_samples.sh").exists()
    assert not (repo / "builder-ran.txt").exists()


ALREADY_PASSING = (
    'mkdir -p tests\necho \'test "$(sh greet.sh ann)" = "hello ann"\' > tests/check_ann.sh\n'
)


async def _already_built(client, origin) -> tuple[dict, dict]:
    p = make_project(client, origin)
    first = add_task(client, p["id"], scripts(), title="Greet")
    t = add_task(
        client,
        p["id"],
        scripts(tester=ALREADY_PASSING, builder=NO_BUILDER),
        title="Greet Ann",
        deps=[first["id"]],
    )
    waiting = {*TERMINAL, TaskState.BLOCKED}
    await drive(Orchestrator(), lambda: task(t["id"]).state in waiting, timeout=60)
    return p, t


async def test_tests_that_already_pass_ask_the_user_and_can_merge(client, origin):
    p, t = await _already_built(client, origin)
    row = task(t["id"])
    assert row.state == TaskState.BLOCKED and "already pass" in row.reason
    gate = next(g for g in client.get("/api/gates").json() if g["task_id"] == t["id"])
    assert gate["kind"] == "already_built" and gate["state"] == "pending"
    client.post(f"/api/gates/{gate['id']}/resolve", json={"approve": True})
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=60)
    row = task(t["id"])
    assert row.state == TaskState.DONE, row.reason
    with session() as s:
        started = [e.message for e in s.exec(select(Event).where(Event.task_id == t["id"]))]
    assert not any("Builder Agent starting" in m for m in started)
    repo = gitops.repo_dir(p["id"])
    assert (repo / "tests" / "check_ann.sh").exists() and not (repo / "builder-ran.txt").exists()


async def test_tests_that_already_pass_can_go_back_to_the_tester(client, origin):
    _, t = await _already_built(client, origin)
    gate = next(g for g in client.get("/api/gates").json() if g["task_id"] == t["id"])
    client.post(
        f"/api/gates/{gate['id']}/resolve", json={"approve": False, "note": "test Ann's title"}
    )
    await drive(Orchestrator(), lambda: task(t["id"]).state != TaskState.BLOCKED, timeout=30)
    row = task(t["id"])
    assert not row.tests_ready and not row.no_build
    assert row.state in (TaskState.QUEUED, TaskState.RUNNING, *TERMINAL)


# A runner that also runs the acceptance tests, as older projects' runners did.
RUNNER_ALL = RUNNER.replace(
    "for t in tests/check_*.sh; do",
    'for t in tests/check_*.sh tests/acceptance/check_*.sh; do\n  [ -f "$t" ] || continue',
)
# An acceptance test that fails until the project is done.
UNFINISHED = "mkdir -p tests/acceptance\necho false > tests/acceptance/check_done.sh\n"
ACCEPTANCE_RUN = "echo 'sh tests/acceptance/check_done.sh' > tests/acceptance/run.sh\n"


async def test_acceptance_tests_never_count_for_a_task(client, origin):
    p = make_project(client, origin)
    tester = RUNNER_ALL + UNFINISHED + TESTER.removeprefix(RUNNER)
    t = add_task(client, p["id"], scripts(tester=tester))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason
    green = test_runs_of(t["id"])[-1]
    assert (green.step, green.passed, green.failed) == ("builder", 1, 0)
    [summary] = [e for e in events(t["id"], "test") if e.message.startswith("tests (builder)")]
    assert "1 acceptance tests not counted" in summary.message
    assert "## Acceptance tests" in pack_of(runs_of(t["id"])[-1])


async def test_the_acceptance_tests_stay_out_of_the_suite(client, origin):
    p = make_project(client, origin)
    leaky = RUNNER_ALL + UNFINISHED + ACCEPTANCE_RUN + TESTER.removeprefix(RUNNER)
    t = add_task(client, p["id"], scripts(tester=leaky), kind="acceptance")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    row = task(t["id"])
    assert row.state == TaskState.FAILED and "also runs the acceptance tests" in row.reason
    assert "Keep them out of `tests/run.sh`" in pack_of(runs_of(t["id"])[0])
    assert "Never invent function names" in pack_of(runs_of(t["id"])[0])

    apart = RUNNER + UNFINISHED + ACCEPTANCE_RUN
    t = add_task(client, p["id"], scripts(tester=apart), kind="acceptance")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason


async def test_setup_only_reports_coverage(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(builder=LOW), kind="setup")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason
    assert [r.step for r in runs_of(t["id"])] == ["tester", "builder"]
    assert test_runs_of(t["id"])[-1].changed_coverage is not None


async def test_coverage_minimum_zero_only_reports(client, origin):
    p = make_project(client, origin)
    client.patch(f"/api/projects/{p['id']}", json={"min_task_coverage": 0})
    t = add_task(client, p["id"], scripts(builder=LOW))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE


async def test_blocked_task_can_go_back_to_the_tester(client, origin):
    p = make_project(client, origin, watchdog={"max_retries": 0, "split": False})
    t = add_task(client, p["id"], scripts(builder="echo 'echo bye' > greet.sh"))
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state == TaskState.BLOCKED)
    assert task(t["id"]).tests_ready
    with session() as s:
        gate = s.exec(select(Gate).where(Gate.kind == GateKind.ESCALATION)).one()
    r = client.post(f"/api/gates/{gate.id}/resolve", json={"approve": True, "action": "send_back"})
    assert r.status_code == 200
    await orch.tick()
    row = task(t["id"])
    assert not row.tests_ready and row.state in (TaskState.QUEUED, *TERMINAL, TaskState.RUNNING)
    with session() as s:
        assert s.get(Gate, gate.id).state == GateState.APPROVED  # type: ignore[union-attr]


async def test_new_tests_must_fail_first(client, origin):
    p = make_project(client, origin)
    passing = RUNNER + "echo true > tests/check_already.sh\n"
    t = add_task(client, p["id"], scripts(tester=passing))
    waiting = {*TERMINAL, TaskState.BLOCKED}
    await drive(Orchestrator(), lambda: task(t["id"]).state in waiting)
    row = task(t["id"])
    # Tests that pass before any work: the user decides whether it's already built.
    assert row.state == TaskState.BLOCKED and "already pass" in row.reason
    assert [r.step for r in runs_of(t["id"])] == ["tester"]
    assert [g["kind"] for g in client.get("/api/gates").json()] == ["already_built"]


async def test_repair_tasks_hear_which_acceptance_tests_fail(client, origin):
    p = make_project(client, origin)
    planned = add_task(client, p["id"], scripts())  # added before the acceptance run
    with session() as s:
        run = TestRun(project_id=p["id"], step="acceptance", passed=3, failed=1)
        s.add(run)
        s.commit()
        s.add(
            TestCase(
                test_run_id=run.id,
                name="test_shares",
                classname="tests.acceptance.t",
                status="failed",
                message="expected a (name, cents) pair, got int",
            )
        )
        s.commit()
    fix = scripts(
        tester=RUNNER + "echo 'test -f fixed.txt' > tests/check_fixed.sh\n",
        builder="echo fixed > fixed.txt\n",
    )
    repair = add_task(client, p["id"], fix, origin="replan")
    await drive(
        Orchestrator(), lambda: all(task(x["id"]).state in TERMINAL for x in (repair, planned))
    )
    tester, builder = runs_of(repair["id"])
    assert "`tests.acceptance.t::test_shares`: expected a (name, cents) pair" in pack_of(tester)
    assert "fail now for the same reason" in pack_of(tester)
    assert "sh tests/acceptance/run.sh" in pack_of(builder)
    assert "Failing acceptance tests" not in pack_of(runs_of(planned["id"])[0])


def set_builder(task_id: int, builder: str) -> None:
    with session() as s:
        row = s.get(Task, task_id)
        assert row is not None
        row.executor_script = scripts(builder=builder)
        s.add(row)
        s.commit()


async def test_every_attempt_starts_from_the_latest_milestone(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(builder="echo 'echo bye' > greet.sh"))
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.FAILED
    gitops.write_and_commit(p["id"], {"other.txt": "another task's work"}, "Merge task 99")
    set_builder(t["id"], BUILDER)
    client.post(f"/api/tasks/{t['id']}/retry", json={})
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason
    assert [r.step for r in runs_of(t["id"])] == ["tester", "builder", "builder"]
    assert any("brought in the latest" in e.message for e in events(t["id"], "system"))
    clone = gitops.task_dir(p["id"], t["id"])
    assert (clone / "other.txt").exists()
    assert "other.txt" not in gitops.changed_files(clone)  # only the task's own changes


async def test_a_conflict_with_the_milestone_asks_at_once(client, origin):
    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(builder="echo 'echo bye' > greet.sh"))
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    # Another task merged its own version of the same files meanwhile.
    gitops.write_and_commit(p["id"], {"greet.sh": "echo hi", "tests/run.sh": "exit 1"}, "x")
    client.patch(f"/api/projects/{p['id']}", json={"watchdog": {"escalation": True}})
    set_builder(t["id"], BUILDER)
    client.post(f"/api/tasks/{t['id']}/retry", json={})
    await drive(orch, lambda: task(t["id"]).state == TaskState.BLOCKED)
    runs = runs_of(t["id"])
    assert len(runs) == 3 and runs[-1].failure_kind == "conflict"  # no retries burnt
    assert "greet.sh" in runs[-1].reason and "tests/run.sh" in runs[-1].reason
    with session() as s:
        gate = s.exec(select(Gate).where(Gate.kind == GateKind.ESCALATION)).one()
    assert "start the task over" in gate.detail
    # Approving starts over from the milestone branch: Tester and Builder again.
    client.post(f"/api/gates/{gate.id}/resolve", json={"approve": True})
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason
    assert [r.step for r in runs_of(t["id"])][3:] == ["tester", "builder"]


def test_agent_tasks_take_criteria_not_commands(client, origin):
    p = make_project(client, origin, executor_kind="opencode")
    base = {"title": "x", "description": "goal"}
    r = client.post(f"/api/projects/{p['id']}/tasks", json={**base, "validation_cmd": "pytest"})
    assert r.status_code == 400 and "the Tester writes their tests" in r.json()["detail"]
    r = client.post(f"/api/projects/{p['id']}/tasks", json=base)
    assert r.status_code == 400 and "acceptance criterion" in r.json()["detail"]
    r = client.post(f"/api/projects/{p['id']}/tasks", json={**base, "criteria": ["works"]})
    assert r.status_code == 201 and r.json()["criteria"] == ["works"]


def test_roles_are_listed(client):
    names = [r["name"] for r in client.get("/api/roles").json()]
    assert names == [
        "Project Planning Agent",
        "Task Planning Agent",
        "Environment Agent",
        "Tester Agent",
        "Builder Agent",
        "Reviewer Agent",
    ]
