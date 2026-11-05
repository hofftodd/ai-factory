import pytest

from foreman.core.models import Project
from foreman.planner import (
    PlanError,
    chat_stage,
    chat_system_prompt,
    find_cycle,
    parse_chat,
    parse_tasks,
    render_tasks_md,
    topo_order,
    writable,
)

P = Project(name="todo", repo_url="x", feature="001-initial")


def test_parse_chat_extracts_files_and_command():
    text = """I drafted the spec.

<<<FILE specs/001-initial/spec.md
# Spec
- FR-1.1 Add todos.
>>>

<<<FILE tests/acceptance/test_todos.py
def test_add(): assert False
>>>

<<<ACCEPTANCE_CMD
pytest -q tests/acceptance
>>>

What storage do you want?"""
    out = parse_chat(P, text)
    assert out.files == {
        "specs/001-initial/spec.md": "# Spec\n- FR-1.1 Add todos.",
        "tests/acceptance/test_todos.py": "def test_add(): assert False",
    }
    assert out.acceptance_cmd == "pytest -q tests/acceptance"
    assert "[wrote specs/001-initial/spec.md]" in out.message
    assert "What storage do you want?" in out.message
    assert "FR-1.1" not in out.message  # file bodies are not repeated in the chat


def test_parse_chat_rejects_writes_outside_allowlist():
    text = "<<<FILE src/app.py\nx\n>>>\n<<<FILE ../../etc/passwd\nx\n>>>\n"
    out = parse_chat(P, text)
    assert out.files == {}
    assert out.rejected == ["src/app.py", "../../etc/passwd"]


def test_writable():
    assert writable(P, "specs/001-initial/plan.md")
    assert writable(P, "tests/acceptance/sub/test_x.py")
    assert writable(P, ".foreman/Dockerfile")
    assert not writable(P, "specs/001-initial/nested/x.md")
    assert not writable(P, "specs/other/spec.md")
    assert not writable(P, "src/main.py")


TASKS = """Here you go:
```json
[
  {"key": "T2", "title": "API", "allowed_files": ["src/api.py"], "deps": ["T1"],
   "validation_cmd": "pytest -q tests/test_api.py", "req_refs": "FR-1.2"},
  {"key": "T1", "title": "Store", "allowed_files": ["src/store.py"], "deps": [],
   "validation_cmd": "pytest -q tests/test_store.py"}
]
```"""


def test_parse_tasks_and_order():
    tasks = parse_tasks(TASKS)
    assert [t.key for t in topo_order(tasks)] == ["T1", "T2"]
    assert tasks[0].req_refs == ["FR-1.2"]  # a bare string is accepted as a list


@pytest.mark.parametrize(
    "text, problem",
    [
        ("no json here", "no JSON array"),
        ("[1, 2", "no JSON array"),
        ("[]", "non-empty"),
        ('[{"key": "A", "title": "a", "deps": ["B"]}]', "unknown deps B"),
        ('[{"key": "A", "title": "a"}, {"key": "A", "title": "b"}]', "duplicate keys"),
        (
            '[{"key": "A", "title": "a", "deps": ["B"]}, {"key": "B", "title": "b", '
            '"deps": ["A"]}]',
            "dependency cycle",
        ),
        ('[{"key": "A"}]', "missing title"),
    ],
)
def test_parse_tasks_problems(text, problem):
    with pytest.raises(PlanError, match=problem):
        parse_tasks(text)


def test_find_cycle():
    assert find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]
    assert find_cycle({"a": ["b"], "b": []}) == []


def test_render_tasks_md():
    md = render_tasks_md(
        P,
        [
            {
                "key": "T1",
                "title": "Store",
                "description": "d",
                "req_refs": ["FR-1.1"],
                "deps": [],
                "allowed_files": ["src/store.py"],
                "context_files": [],
                "validation_cmd": "pytest",
                "todos": ["x"],
                "violations": ["too big"],
            }
        ],
    )
    assert "## T1: Store" in md and "`src/store.py`" in md and "- [ ] x" in md
    assert "> Size check: too big" in md


DOCS = {"spec": "- FR-1 x", "plan": "p", "validation": "- AC-1 (FR-1)"}


def test_chat_stage_moves_from_design_to_tests_to_review():
    assert chat_stage({}, []) == "design"
    assert chat_stage({**DOCS, "validation": " "}, ["README.md"]) == "design"
    assert chat_stage(DOCS, ["README.md"]) == "tests"
    assert chat_stage(DOCS, ["README.md", "tests/acceptance/test_x.py"]) == "review"


def test_chat_prompt_holds_tests_back_until_the_design_is_reviewed():
    design = chat_system_prompt(P, {}, [], "")
    assert "Current step: DESIGN." in design and "Do not write acceptance tests" in design
    tests = chat_system_prompt(P, DOCS, [], "")
    assert "Current step: DESIGN REVIEW." in tests and "write the acceptance tests" in tests
    for prompt in (design, tests):
        assert "contain no implementation code" in prompt
        assert "no language runtimes" in prompt and "USER root" in prompt


def test_parse_chat_accepts_separator_style_blocks():
    # How Qwen wrote them: path on the next line, each <<<FILE ends the previous file,
    # and a bare <<<FILE before prose only terminates.
    text = """Here you go.
<<<FILE specs/001-initial/spec.md
# Spec
<<<FILE
specs/001-initial/plan.md
# Plan
<<<FILE

The tests fail for now.

<<<ACCEPTANCE_CMD
node --test
>>>
Done."""
    out = parse_chat(P, text)
    assert out.files == {
        "specs/001-initial/spec.md": "# Spec",
        "specs/001-initial/plan.md": "# Plan",
    }
    assert out.acceptance_cmd == "node --test"
    assert out.message.splitlines() == [
        "Here you go.",
        "[wrote specs/001-initial/spec.md]",
        "[wrote specs/001-initial/plan.md]",
        "",
        "The tests fail for now.",
        "",
        "[acceptance command: node --test]",
        "Done.",
    ]


def test_parse_chat_does_not_apply_a_cut_off_file():
    out = parse_chat(P, "Writing.\n<<<FILE specs/001-initial/spec.md\n# Spe")
    assert out.files == {}
    assert out.message.endswith("[incomplete file specs/001-initial/spec.md: not written]")


def test_parse_chat_bare_terminator_before_prose_is_not_a_path():
    out = parse_chat(P, "<<<FILE specs/001-initial/spec.md\n# S\n<<<FILE\nAll done here.")
    assert out.files == {"specs/001-initial/spec.md": "# S"}
    assert out.message.endswith("All done here.")
