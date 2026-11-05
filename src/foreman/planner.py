"""The planner role: Phase 1 conversation and plan decomposition.

Both are pure functions over text so they can be tested without a model:

- the *chat* protocol lets the planner write documents by emitting blocks::

      <<<FILE specs/001-initial/spec.md
      ...complete file content...
      >>>

      <<<ACCEPTANCE_CMD
      pytest -q tests/acceptance
      >>>

  Full-file blocks (not diffs) are deliberately simple so small local models
  can follow them reliably. The parser is lenient about the variations local
  models produce: the path may be on the line after ``<<<FILE``, and a block
  may end at the next ``<<<FILE``/``<<<ACCEPTANCE_CMD`` instead of ``>>>`` (a
  bare ``<<<FILE`` followed by a blank line is then only a terminator). A
  block still open when the reply ends is not applied: the reply was cut off.

- *decomposition* asks for a JSON array of tasks, validates it (fields,
  dependency graph, size limits via context packs) and returns a list of
  problems to send back to the planner when it needs another try.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from foreman.core.models import Project

DOC_NAMES = ("spec", "plan", "validation", "tasks")

_FILE_OPEN = re.compile(r"^<<<FILE(?:[ \t]+(.*?))?[ \t]*$")
_CMD_OPEN = re.compile(r"^<<<ACCEPTANCE_CMD[ \t]*$")
_CLOSE = ">>>"


def doc_path(project: Project, name: str) -> str:
    return f"specs/{project.feature}/{name}.md"


def writable(project: Project, rel: str) -> bool:
    """Paths the planner may write: the spec docs, acceptance tests, toolchain image."""
    spec_dir = f"specs/{project.feature}/"
    return (
        (rel.startswith(spec_dir) and rel.endswith(".md") and "/" not in rel[len(spec_dir) :])
        or rel.startswith("tests/acceptance/")
        or rel == ".foreman/Dockerfile"
    )


@dataclass
class ChatOutput:
    message: str  # what the planner said, with file blocks replaced by notes
    files: dict[str, str] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    acceptance_cmd: str | None = None


def parse_chat(project: Project, text: str) -> ChatOutput:
    from foreman.gitops import safe_rel_path

    out = ChatOutput(message="")
    message: list[str] = []
    # The open block: ("file", path) or ("cmd", ""), and its lines so far.
    block: tuple[str, str] | None = None
    body: list[str] = []

    def take_file(raw_path: str, content: str) -> str:
        try:
            rel = safe_rel_path(raw_path)
        except ValueError:
            out.rejected.append(raw_path)
            return f"[rejected write to {raw_path}]"
        if not writable(project, rel):
            out.rejected.append(rel)
            return f"[rejected write to {rel}]"
        out.files[rel] = content
        return f"[wrote {rel}]"

    def close() -> None:
        nonlocal block
        if block is None:
            return
        kind, path = block
        if kind == "file":
            message.append(take_file(path, "\n".join(body)))
        else:
            out.acceptance_cmd = "\n".join(body).strip()
            message.append(f"[acceptance command: {out.acceptance_cmd}]")
        block = None
        body.clear()

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if m := _FILE_OPEN.match(line):
            close()
            path = (m.group(1) or "").strip()
            nxt = lines[i].strip() if i < len(lines) else ""
            if not path and nxt and not any(c.isspace() for c in nxt):
                path = nxt  # path on the next line (prose there means a bare terminator)
                i += 1
            if path:
                block = ("file", path)
        elif _CMD_OPEN.match(line):
            close()
            block = ("cmd", "")
        elif block is not None and line.strip() == _CLOSE:
            close()
        elif block is not None:
            body.append(line)
        else:
            message.append(line)
    if block is not None:
        kind, path = block
        message.append(
            f"[incomplete file {path}: not written]" if kind == "file" else "[incomplete command]"
        )
    out.message = "\n".join(message).strip()
    return out


# What `.foreman/Dockerfile` builds on (docker/executor-base/Dockerfile), so the
# planner does not have to guess.
BASE_IMAGE = (
    "`foreman/executor-base` is Ubuntu 24.04 with git, curl, bash and the coding agent, and "
    "no language runtimes (no Node, no Python): the Dockerfile must install what the project "
    "needs. The base image ends with `USER agent`, so switch to `USER root` for apt-get and "
    "back to `USER agent` at the end."
)


def chat_stage(docs: dict[str, str], tree: list[str]) -> str:
    """Where the conversation is: "design" until spec, plan and validation exist,
    "tests" until acceptance tests exist, then "review"."""
    if not all(docs.get(n, "").strip() for n in ("spec", "plan", "validation")):
        return "design"
    if not any(f.startswith("tests/acceptance/") for f in tree):
        return "tests"
    return "review"


_STAGE_GUIDANCE = {
    "design": [
        "Current step: DESIGN. Ask clarifying questions until the request is clear, then "
        "write spec.md, plan.md and validation.md. Do not write acceptance tests, the "
        "Dockerfile or the acceptance command yet: the user reviews the design first. End by "
        "asking the user to review it.",
    ],
    "tests": [
        "Current step: DESIGN REVIEW. The design documents exist. While the user gives "
        "feedback, revise them. Once the user says the design is right (or asks you to "
        "continue), write the acceptance tests, .foreman/Dockerfile and the acceptance command.",
    ],
    "review": [
        "Current step: REVIEW. Documents and acceptance tests exist. Change them as the user "
        "asks, keeping spec, validation and tests consistent with each other.",
    ],
}


def chat_system_prompt(
    project: Project, docs: dict[str, str], tree: list[str], acceptance_cmd: str
) -> str:
    d = f"specs/{project.feature}"
    stage = chat_stage(docs, tree)
    parts = [
        "You are the planner for a software project. You design it with the user; coding "
        "agents implement it later, in small tasks. The deliverables, in order:",
        f"1. {d}/spec.md - requirements. Give every requirement a stable id on its own bullet, "
        "e.g. `- FR-1.1 Users can add a todo.` (functional) or `- NFR-1 ...` (non-functional).",
        f"2. {d}/plan.md - architecture, components, file layout, the interfaces between "
        "components (names, signatures, data shapes), milestones.",
        f"3. {d}/validation.md - acceptance criteria, each citing the requirement ids it checks.",
        "4. Executable acceptance tests under tests/acceptance/ that fail until the software "
        "is built, plus the command that runs them, and .foreman/Dockerfile.",
        "",
        "You design; you do not implement. The documents describe behaviour and interfaces "
        "and contain no implementation code. The only code you write is the acceptance tests "
        "and the Dockerfile.",
        "Keep documents concise: the work will be split into small tasks for coding agents "
        "with small context windows, so prefer small files and clear interfaces.",
        "Think about decisions, not text: do not draft file contents while reasoning. Write "
        "each file once, in your reply.",
        "",
        *_STAGE_GUIDANCE[stage],
        "",
        "To create or replace a file, output a block with the COMPLETE new content:",
        "<<<FILE path/to/file",
        "...complete file content...",
        ">>>",
        f"You may only write {d}/spec.md, {d}/plan.md, {d}/validation.md, files under "
        "tests/acceptance/, and .foreman/Dockerfile (a Dockerfile `FROM foreman/executor-base` "
        "that installs the language toolchain the project needs).",
        BASE_IMAGE,
        "To set the command that runs the acceptance tests:",
        "<<<ACCEPTANCE_CMD",
        "the command",
        ">>>",
        "Outside those blocks, reply to the user briefly: what you changed and what you need "
        "from them next.",
        "",
        "# Current state",
        f"Acceptance command: {acceptance_cmd or '(not set)'}",
    ]
    for name in ("spec", "plan", "validation"):
        content = docs.get(name, "").strip()
        parts += ["", f"## {d}/{name}.md", content or "(empty)"]
    parts += ["", "## Repository files", *(tree[:300] or ["(empty repository)"])]
    if len(tree) > 300:
        parts.append(f"... and {len(tree) - 300} more")
    return "\n".join(parts)


# -- decomposition ---------------------------------------------------------------

TASK_FIELDS = (
    "key",
    "title",
    "description",
    "req_refs",
    "todos",
    "allowed_files",
    "context_files",
    "validation_cmd",
    "deps",
)


def decompose_system_prompt(
    project: Project, docs: dict[str, str], tree: list[str], budget_tokens: int
) -> str:
    d = f"specs/{project.feature}"
    return "\n".join(
        [
            "You are the planner for a software project. Break the approved plan into small, "
            "ordered tasks for coding agents that run on local models with small context windows.",
            "",
            "Rules for every task:",
            f"- It changes at most {project.max_task_files} files, listed explicitly in "
            "allowed_files (no wildcards). Tests it writes count.",
            f"- Its context (requirements, file contents, interfaces) must fit in about "
            f"{budget_tokens} tokens: keep files small; split large changes.",
            "- validation_cmd is a command that exits 0 only when the task is done, normally a "
            "targeted test the task itself writes or that already exists.",
            "- req_refs lists the requirement ids from spec.md it implements.",
            "- context_files lists existing or earlier-task files whose interfaces it needs "
            "(read-only).",
            "- deps lists keys of tasks that must finish first. No cycles.",
            "- The acceptance tests in tests/acceptance/ must pass once all tasks are done.",
            "",
            "Output ONLY a JSON array, no prose, like:",
            '[{"key": "T1", "title": "...", "description": "...", "req_refs": ["FR-1.1"], '
            '"todos": ["..."], "allowed_files": ["src/x.py", "tests/test_x.py"], '
            '"context_files": [], "validation_cmd": "pytest -q tests/test_x.py", "deps": []}]',
            "",
            f"## {d}/spec.md",
            docs.get("spec", "").strip() or "(empty)",
            "",
            f"## {d}/plan.md",
            docs.get("plan", "").strip() or "(empty)",
            "",
            f"## {d}/validation.md",
            docs.get("validation", "").strip() or "(empty)",
            "",
            f"Acceptance command: {project.acceptance_cmd or '(not set)'}",
            "",
            "## Repository files",
            *(tree[:300] or ["(empty repository)"]),
        ]
    )


def split_system_prompt(
    project: Project,
    docs: dict[str, str],
    tree: list[str],
    budget_tokens: int,
    task: dict[str, Any],
    failures: list[str],
) -> str:
    """Ask the planner to split one task that keeps failing into smaller ones."""
    d = f"specs/{project.feature}"
    return "\n".join(
        [
            "You are the planner for a software project. Split ONE task that coding agents "
            "keep failing into 2-4 smaller, ordered tasks that together do the same work.",
            "",
            "Rules for every new task:",
            f"- It changes at most {project.max_task_files} files, listed explicitly in "
            "allowed_files (no wildcards).",
            f"- Its context must fit in about {budget_tokens} tokens.",
            "- validation_cmd exits 0 only when that task is done.",
            "- deps lists keys of the other new tasks that must finish first.",
            "- Address the failures below: smaller steps, clearer interfaces, earlier tests.",
            "",
            "Output ONLY a JSON array in the same format as the task below.",
            "",
            "## Task to split",
            json.dumps(task, indent=2),
            "",
            "## Why it failed",
            *[f"- {f}" for f in failures],
            "",
            f"## {d}/spec.md",
            docs.get("spec", "").strip() or "(empty)",
            "",
            f"## {d}/plan.md",
            docs.get("plan", "").strip() or "(empty)",
            "",
            "## Repository files",
            *(tree[:300] or ["(empty repository)"]),
        ]
    )


class PlanError(ValueError):
    pass


def extract_json_array(text: str) -> list[Any]:
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            raise PlanError("no JSON array found in the response")
        candidate = text[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise PlanError(f"invalid JSON: {e}") from e
    if not isinstance(data, list) or not data:
        raise PlanError("expected a non-empty JSON array of tasks")
    return data


def _str_list(item: dict[str, Any], name: str) -> list[str]:
    v = item.get(name) or []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        raise PlanError(f"task {item.get('key')}: {name} must be a list")
    return [str(x).strip() for x in v if str(x).strip()]


@dataclass
class TaskSpec:
    key: str
    title: str
    description: str
    req_refs: list[str]
    todos: list[str]
    allowed_files: list[str]
    context_files: list[str]
    validation_cmd: str
    deps: list[str]


def parse_tasks(text: str) -> list[TaskSpec]:
    """Parse and structurally validate the planner's task list (raises PlanError)."""
    items = extract_json_array(text)
    tasks: list[TaskSpec] = []
    problems: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"item {i} is not an object")
            continue
        key = str(item.get("key") or f"T{i + 1}").strip()
        try:
            t = TaskSpec(
                key=key,
                title=str(item.get("title") or "").strip(),
                description=str(item.get("description") or "").strip(),
                req_refs=_str_list(item, "req_refs"),
                todos=_str_list(item, "todos"),
                allowed_files=_str_list(item, "allowed_files"),
                context_files=_str_list(item, "context_files"),
                validation_cmd=str(item.get("validation_cmd") or "").strip(),
                deps=_str_list(item, "deps"),
            )
        except PlanError as e:
            problems.append(str(e))
            continue
        if not t.title:
            problems.append(f"task {key}: missing title")
        tasks.append(t)

    keys = [t.key for t in tasks]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        problems.append(f"duplicate keys: {', '.join(sorted(dupes))}")
    known = set(keys)
    for t in tasks:
        unknown = [d for d in t.deps if d not in known]
        if unknown:
            problems.append(f"task {t.key}: unknown deps {', '.join(unknown)}")
        if t.key in t.deps:
            problems.append(f"task {t.key}: depends on itself")
    if not problems:
        cycle = find_cycle({t.key: t.deps for t in tasks})
        if cycle:
            problems.append(f"dependency cycle: {' -> '.join(cycle)}")
    if problems:
        raise PlanError("; ".join(problems))
    return tasks


def find_cycle(graph: dict[str, list[str]]) -> list[str]:
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(n: str) -> list[str]:
        state[n] = 1
        path.append(n)
        for d in graph.get(n, []):
            if state.get(d) == 1:
                return [*path[path.index(d) :], d]
            if state.get(d) is None:
                found = visit(d)
                if found:
                    return found
        state[n] = 2
        path.pop()
        return []

    for node in graph:
        if state.get(node) is None:
            found = visit(node)
            if found:
                return found
    return []


def topo_order(tasks: list[TaskSpec]) -> list[TaskSpec]:
    by_key = {t.key: t for t in tasks}
    done: set[str] = set()
    out: list[TaskSpec] = []

    def visit(t: TaskSpec) -> None:
        if t.key in done:
            return
        done.add(t.key)
        for d in t.deps:
            visit(by_key[d])
        out.append(t)

    for t in tasks:
        visit(t)
    return out


def retry_message(problems: list[str]) -> str:
    return (
        "Your task list has problems:\n"
        + "\n".join(f"- {p}" for p in problems)
        + "\nFix them, splitting tasks that are too large, and output the COMPLETE corrected "
        "JSON array only."
    )


def render_tasks_md(project: Project, rows: list[dict[str, Any]]) -> str:
    """specs/<feature>/tasks.md: the human-readable task list kept in the repo."""
    lines = [
        f"# Tasks: {project.name}",
        "",
        "Generated by foreman from plan.md. Each task is small enough for one agent run.",
        "",
    ]
    for r in rows:
        lines += [f"## {r['key']}: {r['title']}", ""]
        if r.get("description"):
            lines += [r["description"], ""]
        meta = []
        if r.get("req_refs"):
            meta.append(f"- Requirements: {', '.join(r['req_refs'])}")
        if r.get("deps"):
            meta.append(f"- Depends on: {', '.join(r['deps'])}")
        meta.append(f"- Files: {', '.join(f'`{f}`' for f in r['allowed_files']) or '(none)'}")
        if r.get("context_files"):
            meta.append(f"- Uses: {', '.join(f'`{f}`' for f in r['context_files'])}")
        meta.append(f"- Done when: `{r['validation_cmd'] or '(missing)'}`")
        if r.get("state"):
            meta.append(f"- State: {r['state']}")
        lines += meta
        if r.get("todos"):
            lines += ["", *[f"- [ ] {t}" for t in r["todos"]]]
        if r.get("violations"):
            lines += ["", f"> Size check: {'; '.join(r['violations'])}"]
        lines.append("")
    return "\n".join(lines)
