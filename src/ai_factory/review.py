"""The Reviewer Agent: after a task's tests pass, check the change before it merges.

Passing tests are not enough. In the future-lab run a "visual graph editor" that listed
nodes as text passed its tests, the design's React Flow was never used, and one module
grew to 1,500 lines holding storage, CRUD and import/export. The Reviewer reads the
task, the requirements it covers, the design and the change (with read-only access to
the task's clone) and either passes it or lists what blocks the merge.

Only these block: the design done another way, the goal met only to the letter, code
put where it doesn't belong (or a file over `max_file_lines`), a clear bug the tests
miss, and a user interface a person couldn't use. Everything else is a note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ai_factory.core.contextpack import requirement_slice
from ai_factory.core.models import Project, Task

# The diff shown in full up to this many characters; the Reviewer reads the rest itself.
MAX_DIFF_CHARS = 60_000
DEFAULT_MAX_FILE_LINES = 800


class ReviewError(ValueError):
    pass


@dataclass
class Review:
    passed: bool
    blocking: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def findings(self) -> str:
        """The blocking findings, for the Builder (they become the run's reason)."""
        return "\n".join(f"- {b}" for b in self.blocking)


def big_files(clone: Path, files: list[str], max_lines: int) -> list[tuple[str, int]]:
    """Changed files longer than `max_lines`, longest first."""
    out: list[tuple[str, int]] = []
    for rel in files:
        path = clone / rel
        try:
            n = len(path.read_text(errors="replace").splitlines())
        except (OSError, UnicodeError):
            continue
        if n > max_lines:
            out.append((rel, n))
    return sorted(out, key=lambda x: -x[1])


def system_prompt(
    project: Project,
    task: Task,
    requirements: str,
    design: str,
    diff: str,
    big: list[tuple[str, int]],
    max_lines: int,
) -> str:
    lines = [
        "You are the Reviewer Agent. The Tester wrote tests for the task below and the "
        "Builder made them pass. Passing tests are not enough: review the change the way a "
        "senior engineer would before it merges. You can read the repository (read-only) "
        "with list_files, read_file and search_repo; read what you need beyond the diff.",
        "",
        'Block the merge (put it under "blocking") only for:',
        "- The design says how something is built (a library, a component, a structure, a "
        "data model) and the change does it another way.",
        "- The task's goal is met only to the letter: the tests pass, but the feature isn't "
        'what the goal and requirements describe (a "graph editor" that lists nodes as '
        'text, an "editor" that can\'t edit, a "search" that only matches exact ids).',
        "- Code put where it doesn't belong: in a module whose job is something else, or a "
        f"file grown past {max_lines} lines that should be split by responsibility.",
        "- A clear bug the tests miss: a wrong result, lost data, an error a person would "
        "hit, a security hole, internal values leaking into what users or APIs see.",
        "- A user interface a person couldn't use: internal names shown as labels, no "
        "visual structure or styling, nothing to find their way with.",
        "Everything else (style, naming, small improvements, more tests) is a note, not "
        "blocking. Be specific: name the file and say what to change. Don't block for "
        "missing tests; that's the Tester's job.",
        "",
        "Answer with ONLY a JSON object:",
        '{"verdict": "pass" | "changes", "blocking": ["..."], "notes": ["..."]}',
        '"changes" exactly when "blocking" is not empty.',
        "",
        f"# Task {task.key or task.id}: {task.title}",
        task.description.strip(),
    ]
    if task.criteria:
        lines += ["", "Done when:", *[f"- {c}" for c in task.criteria]]
    if requirements.strip():
        lines += ["", "## Requirements it covers", requirements.strip()]
    if design.strip():
        lines += ["", "## Design", design.strip()]
    if big:
        lines += ["", f"## Changed files over {max_lines} lines"]
        lines += [f"- {rel}: {n} lines" for rel, n in big]
    shown = diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS]
    lines += ["", "## The change", "```diff", shown.rstrip(), "```"]
    if len(diff) > MAX_DIFF_CHARS:
        lines.append(
            f"(The diff is {len(diff)} characters; the first {MAX_DIFF_CHARS} are shown. "
            "Read the changed files for the rest.)"
        )
    return "\n".join(lines)


def requirements_for(task: Task, requirements_doc: str) -> str:
    """The requirements the task covers (the whole document for a task citing none)."""
    if not task.req_refs:
        return requirements_doc
    text, _ = requirement_slice(requirements_doc, task.req_refs)
    return text


def parse_review(text: str) -> Review:
    from ai_factory.orchestrator.replan import extract_json_object
    from ai_factory.planner import PlanError

    try:
        data = extract_json_object(text)
    except PlanError as e:
        raise ReviewError(str(e)) from e
    verdict = str(data.get("verdict") or "").strip().lower()
    blocking = [str(b).strip() for b in data.get("blocking") or [] if str(b).strip()]
    notes = [str(n).strip() for n in data.get("notes") or [] if str(n).strip()]
    if verdict not in ("pass", "changes"):
        raise ReviewError(f'"verdict" must be "pass" or "changes", not {verdict!r}')
    # Blocking findings decide, whatever the verdict says.
    return Review(passed=not blocking, blocking=blocking, notes=notes)
