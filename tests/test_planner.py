import json

import pytest

from ai_factory.core.models import Project
from ai_factory.planner import (
    ACCEPTANCE,
    DOCS,
    SEED,
    SETUP,
    PlanError,
    chat_stage,
    chat_system_prompt,
    decompose_system_prompt,
    find_cycle,
    parse_chat,
    parse_tasks,
    render_tasks_md,
    standard_tasks,
    topo_order,
    wants_demo_data,
    writable,
)

P = Project(name="todo", repo_url="x", feature="001-initial")
D = "specs/001-initial"


def test_parse_chat_extracts_documents():
    text = f"""I drafted the requirements.

<<<FILE {D}/requirements.md
# Requirements
- FR-1.1 Add todos.
>>>

What storage do you want?"""
    out = parse_chat(P, text)
    assert out.files == {f"{D}/requirements.md": "# Requirements\n- FR-1.1 Add todos."}
    assert f"[wrote {D}/requirements.md]" in out.message
    assert "What storage do you want?" in out.message
    assert "FR-1.1" not in out.message  # file bodies are not repeated in the chat


def test_planner_may_not_write_code_tests_or_commands():
    text = (
        "<<<FILE src/app.py\nx\n>>>\n"
        "<<<FILE tests/acceptance/test_x.py\nx\n>>>\n"
        "<<<FILE .ai-factory/Dockerfile\nFROM x\n>>>\n"
        "<<<FILE ../../etc/passwd\nx\n>>>\n"
        "<<<ACCEPTANCE_CMD\npytest\n>>>\n"
    )
    out = parse_chat(P, text)
    assert out.files == {}
    assert out.rejected == [
        "src/app.py",
        "tests/acceptance/test_x.py",
        ".ai-factory/Dockerfile",
        "../../etc/passwd",
        "an acceptance command",
    ]


def test_writable_is_the_three_planning_documents():
    for name in ("requirements.md", "design.md", "acceptance-criteria.md"):
        assert writable(P, f"{D}/{name}")
    assert not writable(P, f"{D}/tasks.md")  # ai-factory keeps the task list
    assert not writable(P, f"{D}/notes.md")
    assert not writable(P, "specs/other/requirements.md")
    assert not writable(P, "tests/acceptance/test_x.py")
    assert not writable(P, "src/main.py")


TASKS = """Here you go:
```json
[
  {"key": "T2", "title": "Search", "goal": "Find todos by text.", "deps": ["T1"],
   "criteria": ["Searching shows matching todos"], "req_refs": "FR-1.2"},
  {"key": "T1", "title": "Store", "goal": "Todos are kept between runs.",
   "criteria": ["A saved todo is there after a restart"], "size": "medium"}
]
```"""


def test_parse_tasks_and_order():
    tasks = parse_tasks(TASKS)
    assert [t.key for t in topo_order(tasks)] == ["T1", "T2"]
    assert tasks[0].req_refs == ["FR-1.2"]  # a bare string is accepted as a list
    assert tasks[0].goal == "Find todos by text." and tasks[1].size == "medium"


def task(**kw):
    return {"key": "A", "title": "a", "goal": "g", "criteria": ["c"], **kw}


@pytest.mark.parametrize(
    "items, problem",
    [
        ([task(deps=["B"])], "unknown deps B"),
        ([task(), task(title="b")], "duplicate or reserved keys"),
        ([task(key="SETUP")], "duplicate or reserved keys"),
        ([task(deps=["B"]), task(key="B", deps=["A"])], "dependency cycle"),
        ([{"key": "A", "goal": "g", "criteria": ["c"]}], "missing title"),
        ([task(criteria=[])], "at least one acceptance criterion"),
        ([task(criteria=[str(i) for i in range(7)])], "7 criteria"),
        ([task(size="large")], "too big; split it"),
        ([task(allowed_files=["src/a.py"])], "don't give allowed_files"),
        ([task(validation_cmd="pytest")], "don't give validation_cmd"),
    ],
)
def test_parse_tasks_problems(items, problem):
    with pytest.raises(PlanError, match=problem):
        parse_tasks(json.dumps(items))


@pytest.mark.parametrize(
    "text, problem",
    [("no json here", "no JSON array"), ("[1, 2", "no JSON array"), ("[]", "non-empty")],
)
def test_parse_tasks_bad_json(text, problem):
    with pytest.raises(PlanError, match=problem):
        parse_tasks(text)


def test_standard_tasks_frame_the_plan():
    planned = standard_tasks(parse_tasks(TASKS))
    keys = [t.key for t in planned]
    assert keys[0] == SETUP and keys[1] == ACCEPTANCE and keys[-1] == DOCS
    by_key = {t.key: t for t in planned}
    assert by_key["T1"].deps == [SETUP]  # everything waits for setup
    assert by_key["T2"].deps == ["T1"]
    assert set(by_key[DOCS].deps) == {ACCEPTANCE, "T1", "T2"}  # docs come last
    assert [t.kind for t in planned] == ["setup", "acceptance", "feature", "feature", "docs"]
    assert any("README.md" in c for c in by_key[SETUP].criteria)


def test_replan_keeps_finished_standard_tasks():
    done = {SETUP: "setup", ACCEPTANCE: "acceptance", DOCS: "docs", "T9": "feature"}
    features = parse_tasks(TASKS, done=("T9",))
    planned = standard_tasks(features, done, acceptance_changed=False)
    assert [t.key for t in planned] == ["T2", "T1", "DOCS-2"]  # no setup, no acceptance
    by_key = {t.key: t for t in planned}
    assert by_key["T1"].deps == [] and set(by_key["DOCS-2"].deps) == {"T1", "T2"}
    changed = standard_tasks(features, done, acceptance_changed=True)
    assert [t.key for t in changed] == ["ACCEPT-2", "T2", "T1", "DOCS-2"]
    assert changed[0].title == "Update the acceptance tests"


def test_demo_data_comes_after_the_features_and_before_the_docs():
    planned = standard_tasks(parse_tasks(TASKS), demo_data=True)
    assert [t.key for t in planned][-2:] == [SEED, DOCS]
    by_key = {t.key: t for t in planned}
    assert set(by_key[SEED].deps) == {"T1", "T2"}
    assert SEED in by_key[DOCS].deps
    assert by_key[SEED].kind == "seed" and by_key[SEED].title == "Demo data"
    assert any("scripts/seed.sh" in c for c in by_key[SEED].criteria)


def test_no_demo_data_task_unless_the_requirements_ask_for_it():
    assert SEED not in [t.key for t in standard_tasks(parse_tasks(TASKS))]


def test_replan_updates_the_demo_data():
    done = {SETUP: "setup", ACCEPTANCE: "acceptance", SEED: "seed", DOCS: "docs"}
    planned = standard_tasks(parse_tasks(TASKS), done, False, demo_data=True)
    assert [t.key for t in planned][-2:] == ["SEED-2", "DOCS-2"]
    assert planned[-2].title == "Update the demo data"


@pytest.mark.parametrize(
    ("requirements", "wanted"),
    [
        ("# R\n\n## Demo data\n- 20 recipes", True),
        ("# R\n\n### demo data\n", True),
        ("# R\n\n## Users and workflows\n- lab techs", False),
        ("We have no demo data heading here.", False),
    ],
)
def test_the_demo_data_section_asks_for_a_seed_task(requirements, wanted):
    assert wants_demo_data(requirements) is wanted


def test_the_project_planner_asks_about_users_workflows_and_demo_data():
    design = chat_system_prompt(P, {}, [])
    assert "## Users and workflows" in design and "## Demo data" in design
    assert "design system" in design
    assert "A generated screen per data type is not a user interface" in design


def test_replan_reserves_finished_keys_and_drops_deps_on_them():
    items = [{"key": "T5", "title": "x", "criteria": ["c"], "deps": ["T1"]}]
    (t,) = parse_tasks(json.dumps(items), done=("T1",))
    assert t.deps == []
    with pytest.raises(PlanError, match="duplicate or reserved keys: T1"):
        parse_tasks(json.dumps([{**items[0], "key": "T1"}]), done=("T1",))
    prompt = decompose_system_prompt(P, {}, [{"key": "T1", "title": "Done thing"}])
    assert "## Already done" in prompt and "Done thing" in prompt


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
                "goal": "Keep todos.",
                "criteria": ["A todo survives a restart"],
                "req_refs": ["FR-1.1"],
                "deps": [],
                "kind": "feature",
                "violations": ["too big"],
            }
        ],
    )
    assert "## T1: Store" in md and "Keep todos." in md
    assert "Done when:\n- A todo survives a restart" in md
    assert "> Size check: too big" in md


PLANNING = {
    "requirements": "- FR-1 x",
    "design": "Two parts.",
    "acceptance": "- AC-1 (FR-1) works",
}


def test_chat_stage_moves_from_design_to_review():
    assert chat_stage({}) == "design"
    assert chat_stage({**PLANNING, "acceptance": " "}) == "design"
    assert chat_stage(PLANNING) == "review"


def test_planner_prompts_think_in_plain_language():
    design = chat_system_prompt(P, {}, [])
    assert "Current step: DESIGN." in design
    review = chat_system_prompt(P, PLANNING, [])
    assert "Current step: REVIEW." in review and "Two parts." in review
    breakdown = decompose_system_prompt(P, PLANNING)
    for prompt in (design, review, breakdown):
        assert "Never write code, tests, file names or paths, shell commands" in prompt
    assert f"{D}/acceptance-criteria.md" in design
    assert "ai-factory adds the standard tasks itself" in breakdown


def test_parse_chat_accepts_separator_style_blocks():
    # How Qwen wrote them: path on the next line, each <<<FILE ends the previous file,
    # and a bare <<<FILE before prose only terminates.
    text = f"""Here you go.
<<<FILE {D}/requirements.md
# Requirements
<<<FILE
{D}/design.md
# Design
<<<FILE

Have a look.
Done."""
    out = parse_chat(P, text)
    assert out.files == {f"{D}/requirements.md": "# Requirements", f"{D}/design.md": "# Design"}
    assert out.message.splitlines() == [
        "Here you go.",
        f"[wrote {D}/requirements.md]",
        f"[wrote {D}/design.md]",
        "",
        "Have a look.",
        "Done.",
    ]


def test_parse_chat_does_not_apply_a_cut_off_file():
    out = parse_chat(P, f"Writing.\n<<<FILE {D}/design.md\n# Des")
    assert out.files == {}
    assert out.message.endswith(f"[incomplete file {D}/design.md: not written]")


def test_parse_chat_bare_terminator_before_prose_is_not_a_path():
    out = parse_chat(P, f"<<<FILE {D}/design.md\n# S\n<<<FILE\nAll done here.")
    assert out.files == {f"{D}/design.md": "# S"}
    assert out.message.endswith("All done here.")


def test_old_projects_keep_their_document_names(tmp_path, monkeypatch):
    from ai_factory import gitops
    from ai_factory.core import roles

    def exists(rel: str) -> bool:
        return rel in {f"{D}/spec.md", f"{D}/plan.md"}

    assert roles.doc_file(P, "requirements", exists) == f"{D}/spec.md"
    assert roles.doc_file(P, "design", exists) == f"{D}/plan.md"
    assert roles.doc_file(P, "acceptance", exists) == f"{D}/acceptance-criteria.md"
    assert gitops  # imported for the repo-backed variant used by doc_path
