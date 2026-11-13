"""The Environment Agent: one toolchain image for the whole project.

After the plan is approved, and before any Tester or Builder runs, the Environment
Agent reads the planning documents and the task list and writes `.ai-factory/Dockerfile`
(FROM the executor base image) plus `.ai-factory/check.sh`, a script proving every tool
the project needs is installed. Like the planners it has no tools: it answers in
text, and ai-factory does the rest:

1. builds the image on the host (which has internet access, unlike the sandboxes);
2. runs check.sh inside it, as the agent user, with no network;
3. on a failure, shows the agent the output and asks again (MAX_ATTEMPTS in all);
4. once both pass, commits `.ai-factory/` to the milestone branch and starts execution.

Every step of every task then runs in that image (images.ensure_image reuses it), and
`.ai-factory/` is read-only for the Tester and Builder. The agent runs again only when
asked ("Rebuild environment"), or after going back to planning if the requirements or
design changed. Without Docker there are no images, and the step is skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from sqlmodel import col, select

from ai_factory import gitops, images
from ai_factory.config import get_settings
from ai_factory.core import gates, roles
from ai_factory.core.models import (
    JobKind,
    JobState,
    Node,
    PlannerJob,
    Project,
    ProjectPhase,
    Task,
    TaskState,
)
from ai_factory.core.state import emit
from ai_factory.db import session
from ai_factory.llm import Aborted
from ai_factory.planner import BASE_IMAGE, doc_path
from ai_factory.sandbox import Proc, kill_handle

if TYPE_CHECKING:
    from ai_factory.orchestrator.jobs import JobRunner

CHECK_SCRIPT = f"{roles.ENV_DIR}/check.sh"
MAX_ATTEMPTS = 3
CHECK_TIMEOUT_S = 300
OUTPUT_TAIL = 60
ABORT_POLL_S = 1.0

_FILE = re.compile(r"^<<<FILE[ \t]+(\S+)[ \t]*\n(.*?)^>>>[ \t]*$", re.S | re.M)


class EnvError(ValueError):
    pass


def docker_available() -> bool:
    return get_settings().runtime == "docker"


def system_prompt(
    project: Project, docs: dict[str, str], tasks: list[Task], current: dict[str, str], note: str
) -> str:
    parts = [
        "You are the Environment Agent. Prepare the one toolchain image every Tester and "
        "Builder agent of this project works in. They have no internet access and cannot "
        "install anything: whatever the work needs must already be in the image.",
        "",
        "Write two files:",
        f"- `{images.DOCKERFILE}`: FROM ai-factory/executor-base. Install the language runtime "
        "(the version the documents name), the test framework, the coverage tool, and every "
        "build tool and library the documents and tasks call for. Install packages so the "
        "`agent` user can use them without activating anything. Don't add project code. "
        "Use the build tool the design names (Gradle when it says Gradle, not Maven), and "
        "fetch what it needs ahead of time (its distribution and the libraries the design "
        "names) so builds work offline.",
        f"- `{CHECK_SCRIPT}`: a POSIX sh script, one command per tool, proving each one is "
        "installed and works (e.g. printing its version). It must exit non-zero if anything "
        "is missing. It runs inside the image as the `agent` user, with no network.",
        "",
        BASE_IMAGE,
        "ai-factory builds the image and runs the check; if either fails you get the output.",
        "",
        "Output each file as a block with its COMPLETE content:",
        "<<<FILE .ai-factory/Dockerfile",
        "...",
        ">>>",
        f"Only files under `{roles.ENV_DIR}/` are accepted. After the blocks, list in one "
        "or two sentences what the image provides.",
    ]
    parts += [
        "",
        "Install an HTML reporter for each test framework too (e.g. pytest-html, "
        "@vitest/ui, jest-html-reporters), so the test runner can write an HTML report "
        "of the results.",
        "",
        "If the documents describe a web user interface, also install Playwright with its "
        "Chromium and Chromium's system libraries (e.g. `npm install -g playwright` then "
        "`npx playwright install --with-deps chromium`, as root, with PLAYWRIGHT_BROWSERS_PATH "
        "set to a directory the `agent` user can read). ai-factory opens the running app's "
        "pages in it after every task, and the acceptance tests drive the interface with "
        "it. Have the check script prove Chromium launches (e.g. `node -e` with "
        "`require('playwright').chromium.launch()`, with NODE_PATH set to `npm root -g`).",
    ]
    if project.test_note.strip():
        parts += [
            "",
            "## How this project's tests run (from the user)",
            project.test_note.strip(),
            "Install everything that needs, so `tests/run.sh` can wrap it.",
        ]
    if note:
        parts += ["", "## Requested by the user", note]
    if current:
        parts += ["", "## The current toolchain (improve it; keep what still applies)"]
        for rel, text in sorted(current.items()):
            parts += [f"`{rel}`:", "```", text.strip(), "```"]
    parts += ["", "# Planning documents"]
    for key in roles.PLANNING_DOCS:
        parts += ["", f"## {doc_path(project, key)}", docs.get(key, "").strip() or "(empty)"]
    if tasks:
        parts += ["", "## The approved tasks"]
        parts += [f"- {t.key or t.id}: {t.title}. {t.description}".rstrip() for t in tasks]
    return "\n".join(parts)


def parse_files(text: str) -> dict[str, str]:
    """The `.ai-factory/` files in the agent's answer (raises EnvError)."""
    files: dict[str, str] = {}
    for m in _FILE.finditer(text):
        rel = m.group(1).strip()
        rel = rel[2:] if rel.startswith("./") else rel
        if not roles.is_env_path(rel) or ".." in rel.split("/"):
            raise EnvError(f"{rel} is outside {roles.ENV_DIR}/; write only toolchain files")
        files[rel] = m.group(2)
    missing = [f for f in (images.DOCKERFILE, CHECK_SCRIPT) if f not in files]
    if missing:
        raise EnvError(f"missing {', '.join(missing)}; output both files in full")
    images.check_dockerfile(files[images.DOCKERFILE])
    return files


def retry_message(problem: str, output: str) -> str:
    tail = "\n".join(output.splitlines()[-OUTPUT_TAIL:])
    return (
        f"That did not work: {problem}\n"
        + (f"```\n{tail}\n```\n" if tail else "")
        + "Fix it and output the COMPLETE corrected files."
    )


def current_files(project_id: int) -> dict[str, str]:
    root = gitops.repo_dir(project_id) / roles.ENV_DIR
    if not root.is_dir():
        return {}
    out = {}
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.stat().st_size < 64_000:
            out[f.relative_to(root.parent).as_posix()] = f.read_text(errors="replace")
    return out


async def run_check(tag: str, context: Path, on_handle: Callable[[str], None]) -> tuple[int, str]:
    """Run check.sh in the image: as the agent user, without network."""
    s = get_settings()
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm", "--network", "none",
        "--label", "ai-factory=1", "--label", f"ai-factory.instance={s.instance_id}",
        "-v", f"{context}:/ai-factory-env:ro",
        "--entrypoint", "sh", tag, "/ai-factory-env/check.sh",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )  # fmt: skip
    check = Proc(f"local:{proc.pid}", proc)
    on_handle(check.handle)
    lines: list[str] = []

    async def pump() -> int:
        async for line in check.lines():
            lines.append(line)
        return await check.wait()

    try:
        code = await asyncio.wait_for(pump(), CHECK_TIMEOUT_S)
    except TimeoutError:
        await asyncio.to_thread(kill_handle, check.handle)
        return 124, "\n".join([*lines, f"(check timed out after {CHECK_TIMEOUT_S}s)"])
    return code, "\n".join(lines)


async def run_environment(job: JobRunner, node: Node, model: str) -> None:
    """Body of an ENVIRONMENT job."""
    from ai_factory.orchestrator.jobs import JobFailed, finish_job, read_docs

    project = job.project()
    assert project.id is not None
    with session() as s:
        tasks = list(
            s.exec(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    col(Task.state).not_in(
                        (TaskState.DISCARDED, TaskState.SPLIT, TaskState.PROPOSED)
                    ),
                )
                .order_by(col(Task.id))
            ).all()
        )
    note = str(job.payload.get("note", "")).strip()
    system = system_prompt(project, read_docs(project), tasks, current_files(project.id), note)
    work = get_settings().runs_dir / f"environment-{project.id}"
    messages = [{"role": "user", "content": "Prepare the toolchain image now."}]

    async def guarded(coro, handle: dict[str, str]):  # type: ignore[no-untyped-def]
        """Run a build or check; a Stop kills it."""
        task = asyncio.ensure_future(coro)
        while not task.done():
            await asyncio.wait({task}, timeout=ABORT_POLL_S)
            if not task.done() and job.should_abort():
                if handle["current"]:
                    await asyncio.to_thread(kill_handle, handle["current"], 2)
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                raise Aborted()
        return task.result()

    build_lines: list[str] = []

    def flush() -> None:
        if build_lines:
            emit(project.id, None, "log", "\n".join(build_lines), job_id=job.job_id, step="image")
            build_lines.clear()

    def on_output(line: str) -> None:
        build_lines.append(line)
        if len(build_lines) >= 20:
            flush()

    problem = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = await job._call(node, model, system, messages)
        problem, output = await _attempt(project.id, attempt, text, work, guarded, on_output)
        flush()
        if not problem:
            if finish_job(job.job_id, (JobState.RUNNING,), JobState.DONE):
                await asyncio.to_thread(_accept, project, parse_files(text), output)
            return
        emit(project.id, None, "system", f"environment attempt {attempt}: {problem}")
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content": retry_message(problem, output)},
        ]
    raise JobFailed(f"no working toolchain image after {MAX_ATTEMPTS} attempts: {problem}")


async def _attempt(
    project_id: int,
    attempt: int,
    text: str,
    work: Path,
    guarded: Callable,  # type: ignore[type-arg]
    on_output: Callable[[str], None],
) -> tuple[str, str]:
    """Write, build and check one answer. Returns (problem, output); on success the
    problem is "" and the output is the image tag."""
    try:
        files = parse_files(text)
    except (EnvError, images.ImageBuildError) as e:
        return str(e), ""
    shutil.rmtree(work, ignore_errors=True)
    for rel, content in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if content.endswith("\n") else content + "\n")
    emit(project_id, None, "system", f"environment attempt {attempt}: building the image")
    handle = {"current": ""}

    def set_handle(h: str) -> None:
        handle["current"] = h

    try:
        tag = await guarded(
            images.ensure_image(project_id, work, on_output=on_output, on_handle=set_handle),
            handle,
        )
    except images.ImageBuildError as e:
        return str(e), e.output
    emit(project_id, None, "system", f"environment attempt {attempt}: checking {tag}")
    code, output = await guarded(run_check(tag, work / roles.ENV_DIR, set_handle), handle)
    emit(project_id, None, "log", output, step="check")
    if code != 0:
        return f"{CHECK_SCRIPT} failed (exit {code})", output
    return "", tag


def _accept(project: Project, files: dict[str, str], tag: str) -> None:
    """Commit the checked toolchain and, if the project was waiting for it, execute."""
    assert project.id is not None
    sha = gitops.write_and_commit(project.id, files, "environment: toolchain image")
    with session() as s:
        p = s.get(Project, project.id)
        assert p is not None
        p.env_commit = sha
        starting = p.phase == ProjectPhase.ENVIRONMENT
        if starting:
            p.phase = ProjectPhase.EXECUTE
        s.add(p)
        s.commit()
    emit(project.id, None, "system", f"environment ready: {tag}", image=tag)
    if starting:
        emit(project.id, None, "state", "phase: execute", phase="execute")


def failed(project_id: int, error: str) -> None:
    """The job gave up (or was stopped): the project waits in the environment phase."""
    emit(project_id, None, "system", f"environment not ready: {error}")
    with session() as s:
        p = s.get(Project, project_id)
    if p is not None and p.phase == ProjectPhase.ENVIRONMENT:
        gates.notify(project_id, "The toolchain image could not be prepared", error)


def needed(project: Project) -> bool:
    """Does starting execution need the Environment Agent first?"""
    if not docker_available() or not roles.binding(project, roles.ENVIRONMENT):
        return False
    assert project.id is not None
    if not project.env_commit:
        return True
    return any(
        gitops.file_changed_since(project.id, doc_path(project, k), project.env_commit)
        for k in ("requirements", "design")
    )


def queue(project_id: int, note: str = "") -> PlannerJob:
    job = PlannerJob(project_id=project_id, kind=JobKind.ENVIRONMENT, payload={"note": note})
    with session() as s:
        s.add(job)
        s.commit()
        s.refresh(job)
    return job


def start_execution(project_id: int) -> ProjectPhase:
    """After a plan is approved: prepare the environment first if needed, else execute."""
    with session() as s:
        p = s.get(Project, project_id)
        assert p is not None
        phase = ProjectPhase.ENVIRONMENT if needed(p) else ProjectPhase.EXECUTE
        p.phase = phase
        s.add(p)
        s.commit()
    emit(project_id, None, "state", f"phase: {phase.value}", phase=phase.value)
    if phase == ProjectPhase.ENVIRONMENT:
        queue(project_id)
    return phase
