"""The agents and what each one may do.

ai-factory runs five kinds of agent, each with a fixed list of capabilities. The
orchestrator (plain code, no model) is the only component that writes to the
repositories and the database, and it checks every agent action against this
registry before carrying it out. Anything outside an agent's list is refused and
recorded as a ``denied`` event, visible in the dashboard.

Planning agents have no tools at all: they answer in text, and ai-factory parses
only the actions their role allows. Coding agents (Tester, Builder) work in a
sandbox whose mounts already make the forbidden files read-only; the same rules
are checked again on the result before anything is merged.
"""

from __future__ import annotations

import fnmatch
import json
import tomllib
from dataclasses import dataclass
from typing import Any

from ai_factory.core.models import Node, Project, Task

PROJECT_PLANNER = "project_planner"
TASK_PLANNER = "task_planner"
TESTER = "tester"
BUILDER = "builder"
ENVIRONMENT = "environment"
REVIEWER = "reviewer"
ESCALATION = "escalation"

# The toolchain image's directory: only the Environment Agent writes it.
ENV_DIR = ".ai-factory"
# The acceptance tests: run on their own (tests/acceptance/run.sh), never by tests/run.sh.
ACCEPTANCE_DIR = "tests/acceptance"

# Planning documents, by key: human-readable markdown in specs/<feature>/.
DOCS = {
    "requirements": "requirements.md",
    "design": "design.md",
    "acceptance": "acceptance-criteria.md",
    "tasks": "tasks.md",
}
# Projects created before the role split keep their original file names.
LEGACY_DOCS = {"requirements": "spec.md", "design": "plan.md", "acceptance": "validation.md"}
PLANNING_DOCS = ("requirements", "design", "acceptance")


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    summary: str
    may: tuple[str, ...]
    may_not: tuple[str, ...]
    # Binding keys tried in order (older projects used "planner" / "executor").
    bindings: tuple[str, ...]


ROLES: dict[str, Role] = {
    PROJECT_PLANNER: Role(
        PROJECT_PLANNER,
        "Project Planning Agent",
        "Works out the project with you and keeps the planning documents.",
        (
            "write the requirements, design and acceptance criteria (markdown)",
            "talk with the user",
            "read the repository's files (read-only)",
            "research topics on the web (search and read public pages)",
        ),
        ("write code, tests or commands", "create or change tasks"),
        (PROJECT_PLANNER, "planner"),
    ),
    TASK_PLANNER: Role(
        TASK_PLANNER,
        "Task Planning Agent",
        "Reads the planning documents and manages the task list.",
        (
            "read the planning documents",
            "read the repository's files when planning tasks (read-only)",
            "create, split, change and remove tasks (plain language)",
            "propose a change to the planning documents (you decide)",
        ),
        ("write documents, code or tests", "name files or commands"),
        (TASK_PLANNER, "planner"),
    ),
    ENVIRONMENT: Role(
        ENVIRONMENT,
        "Environment Agent",
        "Prepares the toolchain image every Tester and Builder works in, once the plan is "
        "approved.",
        (
            "read the planning documents and the task list",
            "write the toolchain Dockerfile and its check script (.ai-factory/)",
        ),
        ("write code, tests or documents",),
        (ENVIRONMENT, TASK_PLANNER, "planner"),
    ),
    TESTER: Role(
        TESTER,
        "Tester Agent",
        "Writes the tests for each task, and the acceptance tests, and runs them.",
        ("write and change files under the project's test paths", "run commands and tests"),
        ("change any file outside the test paths", "change the toolchain image (.ai-factory/)"),
        (TESTER, "executor"),
    ),
    BUILDER: Role(
        BUILDER,
        "Builder Agent",
        "Writes the code that makes each task's tests pass.",
        (
            "write and change code and docs",
            "run commands, including the tests",
        ),
        (
            "change any test",
            "change the test configuration (except in the setup task)",
            "change the toolchain image (.ai-factory/)",
        ),
        (BUILDER, "executor"),
    ),
    REVIEWER: Role(
        REVIEWER,
        "Reviewer Agent",
        "Reviews each task's change against the requirements and design before it merges.",
        (
            "read the task, the planning documents and the change",
            "read the repository's files (read-only)",
            "send the change back to the Builder with what blocks the merge",
        ),
        ("write code, tests or documents", "change tasks"),
        (REVIEWER, TASK_PLANNER, "planner"),
    ),
}

# Every binding key a project may set (the dashboard shows these).
BINDING_KEYS = (
    PROJECT_PLANNER,
    TASK_PLANNER,
    ENVIRONMENT,
    TESTER,
    BUILDER,
    REVIEWER,
    ESCALATION,
)


def binding(project: Project, role: str) -> dict[str, Any]:
    """The {node_id, model} a role uses in this project ({} if unset)."""
    bindings = project.role_bindings or {}
    keys = ROLES[role].bindings if role in ROLES else (role,)
    for key in keys:
        b = bindings.get(key) or {}
        if b.get("node_id") is not None:
            return dict(b)
    return {}


def choices(project: Project, role: str) -> list[tuple[int, str]]:
    """The (node_id, model) pairs a role may run on, in order of preference: the binding's
    own node and model, then its ``also`` list. Work goes to the first with a free slot."""
    b = binding(project, role)
    out: list[tuple[int, str]] = []
    for c in (b, *(b.get("also") or [])):
        if isinstance(c, dict) and c.get("node_id") is not None:
            pair = (int(c["node_id"]), str(c.get("model") or ""))
            if pair not in out:
                out.append(pair)
    return out


def reasoning_effort(project: Project, task: Task, role: str) -> str:
    """The reasoning effort a coding agent's requests ask for ("" = the model's default).
    An escalated task runs on the escalation binding's node and model, so it uses that
    binding's setting; a node or model set on the task by hand gets the default."""
    b = binding(project, role)
    if task.node_id is not None or task.model:
        esc = binding(project, ESCALATION)
        b = esc if (esc.get("node_id"), esc.get("model")) == (task.node_id, task.model) else {}
    return str(b.get("reasoning_effort") or "")


def node_and_model(project: Project, role: str) -> tuple[Node | None, str]:
    from ai_factory.db import session

    b = binding(project, role)
    node = None
    if b.get("node_id") is not None:
        with session() as s:
            node = s.get(Node, b["node_id"])
    return node, str(b.get("model", ""))


# -- documents ---------------------------------------------------------------------


def doc_file(project: Project, key: str, exists: Any = None) -> str:
    """Repo-relative path of a planning document. `exists(rel) -> bool` lets old
    projects keep their original file names."""
    base = f"specs/{project.feature}/"
    new = base + DOCS[key]
    if key in LEGACY_DOCS and exists is not None:
        old = base + LEGACY_DOCS[key]
        if not exists(new) and exists(old):
            return old
    return new


def planner_may_write(project: Project, rel: str, exists: Any = None) -> bool:
    """The Project Planner writes exactly the three planning documents."""
    return rel in {doc_file(project, k, exists) for k in PLANNING_DOCS}


# -- tests and code ------------------------------------------------------------------

# Test files that can live next to code, outside the test directories.
TEST_FILE_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.spec.ts",
    "*.spec.js",
    "*_spec.rb",
    "*Test.java",
    "*Tests.java",
    "*IT.java",
    "*Test.kt",
    "*Tests.kt",
    "*IT.kt",
    "*Spec.kt",
    "*Test.scala",
    "*Spec.scala",
)

# Maven and Gradle keep every test source and fixture under src/test/ of a module.
JVM_TEST_DIR = "src/test"


def is_test_path(project: Project, rel: str) -> bool:
    dirs = [d.strip("/") for d in (project.test_paths or ["tests"]) if d.strip("/")]
    if any(rel == d or rel.startswith(d + "/") for d in dirs):
        return True
    if rel.startswith(JVM_TEST_DIR + "/") or f"/{JVM_TEST_DIR}/" in rel:
        return True
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) for p in TEST_FILE_PATTERNS)


def is_env_path(rel: str) -> bool:
    return rel == ENV_DIR or rel.startswith(ENV_DIR + "/")


def fence(project: Project, role: str, changed: list[str]) -> list[str]:
    """Files this role changed that it may not touch (empty = within its capabilities)."""
    if role == TESTER:
        return [f for f in changed if not is_test_path(project, f) or is_env_path(f)]
    if role == BUILDER:
        return [f for f in changed if is_test_path(project, f) or is_env_path(f)]
    return []


# Files that only configure how tests run...
TEST_CONFIG_FILES = (
    "conftest.py",
    "pytest.ini",
    ".coveragerc",
    ".rspec",
    ".mocharc.*",
    ".nycrc*",
    "jest.config.*",
    "vitest.config.*",
    "karma.conf.*",
    "phpunit.xml*",
)
# ...and the test sections of files that configure other things too.
TEST_CONFIG_SECTIONS = {
    "pyproject.toml": ("tool.pytest", "tool.coverage"),
    "setup.cfg": ("tool:pytest", "coverage:"),
    "tox.ini": ("pytest", "coverage:"),
    "package.json": ("jest", "mocha", "nyc", "c8", "vitest", "scripts.test"),
}


def is_test_config(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name in TEST_CONFIG_SECTIONS or any(fnmatch.fnmatch(name, p) for p in TEST_CONFIG_FILES)


def test_config_changed(rel: str, before: str | None, after: str | None) -> bool:
    """Whether a change to `rel` changes how tests run (None = the file is absent). The
    Builder may not: skipping or deselecting a test it can't edit would get past the fence."""
    name = rel.rsplit("/", 1)[-1]
    keys = TEST_CONFIG_SECTIONS.get(name)
    if keys is None:
        return is_test_config(rel) and before != after
    return _test_sections(name, before, keys) != _test_sections(name, after, keys)


def _test_sections(name: str, text: str | None, keys: tuple[str, ...]) -> dict[str, Any]:
    if not text:
        return {}
    if name.endswith((".toml", ".json")):
        try:
            data = tomllib.loads(text) if name.endswith(".toml") else json.loads(text)
        except ValueError:  # unparseable: any change counts
            return {"": text}
        return {k: _dig(data, k.split(".")) for k in keys}
    # INI: the lines of each section a key names (a trailing ":" matches a prefix).
    out: dict[str, Any] = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            name_ = line[1:-1].strip()
            hit = any(name_ == k or (k.endswith(":") and name_.startswith(k)) for k in keys)
            section = name_ if hit else None
        elif section and line and not line.startswith(("#", ";")):
            out.setdefault(section, []).append(line)
    return out


def _dig(data: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def denied(project_id: int, task_id: int | None, role: str, what: str, **data: Any) -> None:
    """Record an action an agent tried that its role doesn't allow."""
    from ai_factory.core.state import emit

    emit(
        project_id,
        task_id,
        "denied",
        f"{ROLES[role].name if role in ROLES else role} may not {what}",
        role=role,
        **data,
    )


def describe() -> list[dict[str, Any]]:
    """The registry, for the dashboard."""
    return [
        {
            "key": r.key,
            "name": r.name,
            "summary": r.summary,
            "may": list(r.may),
            "may_not": list(r.may_not),
        }
        for r in ROLES.values()
    ]
