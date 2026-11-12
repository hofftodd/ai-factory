"""The Reviewer Agent's prompt and how its answer is read."""

from __future__ import annotations

import pytest

from ai_factory import review
from ai_factory.core.models import Project, Task

P = Project(name="lab", repo_url="x")
T = Task(
    project_id=1,
    key="T25",
    title="Visual graph editor",
    description="Edit the recipe's procedure as a graph.",
    criteria=["Nodes can be moved"],
    req_refs=["FR-5.2"],
)
REQUIREMENTS = """# Requirements
- FR-5.1 The editor shows the process model as a graph.
- FR-5.2 Nodes and edges can be created, moved, edited and deleted visually.
"""


def test_the_prompt_carries_the_task_the_requirements_the_design_and_the_diff():
    text = review.system_prompt(
        P,
        T,
        review.requirements_for(T, REQUIREMENTS),
        "The graph editor is built on React Flow.",
        "+<div>list of nodes</div>\n",
        [("backend/validation.py", 1562)],
        800,
    )
    assert "You are the Reviewer Agent" in text
    assert "# Task T25: Visual graph editor" in text and "- Nodes can be moved" in text
    assert "FR-5.2" in text and "FR-5.1" not in text  # only what the task covers
    assert "React Flow" in text
    assert "- backend/validation.py: 1562 lines" in text
    assert "+<div>list of nodes</div>" in text


def test_a_long_diff_is_cut_and_says_so():
    diff = "+x\n" * (review.MAX_DIFF_CHARS // 2)
    text = review.system_prompt(P, T, "", "", diff, [], 800)
    assert "Read the changed files for the rest." in text


@pytest.mark.parametrize(
    ("answer", "passed", "blocking"),
    [
        ('{"verdict": "pass", "blocking": [], "notes": ["ok"]}', True, []),
        (
            '```json\n{"verdict": "changes", "blocking": ["a list, not a graph"]}\n```',
            False,
            ["a list, not a graph"],
        ),
        # Blocking findings decide, whatever the verdict says.
        ('{"verdict": "pass", "blocking": ["leaks row ids"]}', False, ["leaks row ids"]),
        ('{"verdict": "changes", "blocking": []}', True, []),
    ],
)
def test_the_answer_is_read(answer, passed, blocking):
    r = review.parse_review(answer)
    assert r.passed is passed and r.blocking == blocking


@pytest.mark.parametrize("answer", ["Looks fine to me.", '{"verdict": "maybe"}'])
def test_an_unreadable_answer_is_an_error(answer):
    with pytest.raises(review.ReviewError):
        review.parse_review(answer)


def test_big_files_lists_changed_files_over_the_limit(tmp_path):
    (tmp_path / "small.py").write_text("x\n" * 10)
    (tmp_path / "big.py").write_text("x\n" * 900)
    assert review.big_files(tmp_path, ["small.py", "big.py", "gone.py"], 800) == [("big.py", 900)]
