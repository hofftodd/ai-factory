"""The Tester -> Builder pipeline: role fences, test results, coverage (local runtime).

Scripted stand-ins play the agents: the Tester's script writes a tiny test runner
(tests/run.sh: every tests/check_*.sh is a test; it writes JUnit XML and LCOV like a
real runner would) and a test; the Builder's script writes the code.
"""

from __future__ import annotations

import json

from sqlmodel import col, select

from foreman import gitops
from foreman.config import get_settings
from foreman.core.models import (
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
from foreman.db import session
from foreman.orchestrator.main import Orchestrator
from tests.helpers import TERMINAL, drive, make_project, task

RUN_SH = r"""out="${FOREMAN_RESULTS:-/tmp}"
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
    from foreman.core.models import Project
    from foreman.db import session as db

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


# The Builder finds tests that can't all pass, and reports instead of working around them.
CONFLICT = BUILDER + (
    """echo 'check_greet contradicts check_old' > "$FOREMAN_RESULTS/tests-conflict.md"\n"""
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
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    row = task(t["id"])
    assert row.state == TaskState.FAILED and "already pass" in row.reason
    assert [r.step for r in runs_of(t["id"])] == ["tester"]
    assert runs_of(t["id"])[0].failure_kind == "no_red"


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
    ]
