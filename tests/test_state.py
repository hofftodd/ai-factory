import pytest

from ai_factory.core.models import Project, Task, TaskState
from ai_factory.core.state import can_transition, estop_active, set_flag, transition
from ai_factory.db import session


def _task() -> int:
    with session() as s:
        p = Project(name="p", repo_url="x")
        s.add(p)
        s.commit()
        t = Task(project_id=p.id, title="t", validation_cmd="true")
        s.add(t)
        s.commit()
        return t.id


def test_transition_is_compare_and_set():
    tid = _task()
    assert transition(tid, (TaskState.QUEUED,), TaskState.RUNNING)
    # Second attempt from the old state loses.
    assert not transition(tid, (TaskState.QUEUED,), TaskState.RUNNING)
    # A stop wins over a later attempt to advance.
    assert transition(tid, (TaskState.RUNNING,), TaskState.STOPPED_BY_USER)
    assert not transition(tid, (TaskState.RUNNING,), TaskState.VALIDATING)


def test_illegal_transition_rejected():
    tid = _task()
    with pytest.raises(ValueError):
        transition(tid, (TaskState.QUEUED,), TaskState.DONE)
    assert not can_transition(TaskState.DONE, TaskState.QUEUED)


def test_estop_flag():
    assert not estop_active()
    set_flag("estop", "1")
    assert estop_active()
    set_flag("estop", "0")
    assert not estop_active()
