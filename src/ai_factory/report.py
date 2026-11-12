"""A project's run report: where the time went and why attempts failed.

The future-lab evaluation had only git history to go on: no token counts, no retries,
no way to tell agent failures from factory bugs, and long stretches with nothing
committed that could have been an agent struggling or ai-factory stuck. This report
answers those from the database, as markdown:

- when the run started and finished, and how much of that something was running;
- tasks by state, splits and replanned additions, runs per role, tokens and spend;
- failed attempts by cause (agent / coordination / factory / setup / limit) and kind;
- per task: wall time, runs per role, failures, tokens;
- stalls: stretches of `STALL_S` or more with no agent or planner running, and whether
  a checkpoint was waiting for the user meanwhile;
- the Reviewer's verdicts and the app checks.

    ai-factory report PROJECT_ID          (also GET /api/projects/{id}/report)
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import col, select

from ai_factory.config import get_settings
from ai_factory.core import causes, roles
from ai_factory.core.models import (
    Event,
    Gate,
    PlannerJob,
    Project,
    RunState,
    Task,
    TaskRun,
    TaskState,
    TestRun,
)
from ai_factory.db import session

# A stretch with nothing running at least this long is a stall worth reporting.
STALL_S = 20 * 60


@dataclass
class _Data:
    project: Project
    tasks: list[Task]
    runs: list[TaskRun]
    jobs: list[PlannerJob]
    gates: list[Gate]
    tests: list[TestRun]
    reviews: list[Event]


def _load(project_id: int) -> _Data:
    with session() as s:
        project = s.get(Project, project_id)
        if project is None:
            raise KeyError(project_id)

        def rows(model, *where, order=None):  # type: ignore[no-untyped-def]
            q = select(model).where(*where)
            return list(s.exec(q.order_by(order if order is not None else col(model.id))))

        return _Data(
            project=project,
            tasks=rows(Task, Task.project_id == project_id),
            runs=rows(TaskRun, TaskRun.project_id == project_id),
            jobs=rows(PlannerJob, PlannerJob.project_id == project_id),
            gates=rows(Gate, Gate.project_id == project_id),
            tests=rows(TestRun, TestRun.project_id == project_id),
            reviews=rows(Event, Event.project_id == project_id, Event.kind == "review"),
        )


def duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rest = divmod(seconds, 3600)
    m, _ = divmod(rest, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h {m:02d}m"
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def stalls(d: _Data, now: float, min_s: float = STALL_S) -> list[tuple[float, float, str]]:
    """Gaps of `min_s` or more between the first and last activity with no task run or
    planner job running: (start, end, what was pending meanwhile)."""
    busy = [(r.started_at, r.ended_at or now) for r in d.runs]
    busy += [(j.started_at, j.ended_at or now) for j in d.jobs if j.started_at]
    merged = _merge(busy)
    out: list[tuple[float, float, str]] = []
    for (_, gap_start), (gap_end, _) in zip(merged, merged[1:], strict=False):
        if gap_end - gap_start < min_s:
            continue
        waiting = [
            g.title
            for g in d.gates
            if g.created_at < gap_end and (g.resolved_at or now) > gap_start
        ]
        why = ("waiting for you: " + "; ".join(waiting[:3])) if waiting else "nothing running"
        out.append((gap_start, gap_end, why))
    return out


def build(project_id: int, now: float | None = None) -> str:
    """The run report as markdown."""
    now = now or time.time()
    d = _load(project_id)
    p = d.project
    lines = [f"# Run report: {p.name}", ""]

    starts = [r.started_at for r in d.runs] + [j.started_at for j in d.jobs if j.started_at]
    ends = [r.ended_at or now for r in d.runs] + [j.ended_at or now for j in d.jobs]
    if not starts:
        return "\n".join([*lines, "Nothing has run yet.", ""])
    first, last = min(starts), max(ends)
    busy = sum(e - s for s, e in _merge(list(zip(starts, ends, strict=True))))
    gaps = stalls(d, now)

    lines += [
        f"- Phase: {p.phase.value}"
        + (f", pull request {p.milestone_pr_url}" if p.milestone_pr_url else ""),
        f"- From {_when(first)} to {_when(last)}: {duration(last - first)}, of which "
        f"{duration(busy)} with an agent or planner running",
        f"- Stalls of {duration(STALL_S)} or more: {len(gaps)}, "
        f"{duration(sum(e - s for s, e, _ in gaps))} in all",
        "",
    ]

    # -- totals -------------------------------------------------------------------------
    by_state = Counter(t.state.value for t in d.tasks)
    failed = [r for r in d.runs if r.state == RunState.FAILED]
    tokens_in = sum(r.tokens_in for r in d.runs) + sum(j.input_tokens for j in d.jobs)
    tokens_out = sum(r.tokens_out for r in d.runs) + sum(j.output_tokens for j in d.jobs)
    steps = Counter(r.step or "single" for r in d.runs)
    lines += [
        "## Totals",
        "",
        "| | |",
        "|---|---|",
        f"| Tasks | {len(d.tasks)} ({', '.join(f'{n} {s}' for s, n in by_state.most_common())}) |",
        f"| Added by splitting | {sum(1 for t in d.tasks if t.origin == 'split')} |",
        f"| Added by replanning | {sum(1 for t in d.tasks if t.origin == 'replan')} |",
        f"| Agent runs | {len(d.runs)} ({', '.join(f'{n} {s}' for s, n in steps.most_common())}) |",
        f"| Failed runs | {len(failed)} |",
        f"| Planner calls | {len(d.jobs)} |",
        f"| Tokens | {tokens_in:,} in, {tokens_out:,} out |",
        f"| Spend | ${p.spend_usd:.2f} |",
        "",
    ]

    # -- failures by cause --------------------------------------------------------------
    by_cause: dict[str, Counter[str]] = defaultdict(Counter)
    for r in failed:
        by_cause[causes.cause_of(r)][r.failure_kind or "?"] += 1
    if failed:
        lines += ["## Failed attempts by cause", "", "| Cause | Runs | Kinds |", "|---|---|---|"]
        for cause in causes.CAUSES:
            kinds = by_cause.get(cause)
            if not kinds:
                continue
            detail = ", ".join(f"{k} {n}" for k, n in kinds.most_common())
            lines.append(
                f"| {cause}: {causes.DESCRIPTION[cause]} | {sum(kinds.values())} | {detail} |"
            )
        lines.append("")

    # -- per task -----------------------------------------------------------------------
    runs_of: dict[int, list[TaskRun]] = defaultdict(list)
    for r in d.runs:
        runs_of[r.task_id].append(r)
    lines += [
        "## Tasks",
        "",
        "| Task | State | Wall time | Tester / Builder / Reviewer | Failed (kinds) | Tokens |",
        "|---|---|---|---|---|---|",
    ]
    for t in d.tasks:
        rs = runs_of.get(t.id or -1, [])
        if not rs and t.state in (TaskState.DISCARDED, TaskState.PROPOSED):
            continue
        wall = max((r.ended_at or now) for r in rs) - min(r.started_at for r in rs) if rs else 0
        per = Counter(r.step for r in rs)
        fails = Counter(r.failure_kind or "?" for r in rs if r.state == RunState.FAILED)
        fail_text = (
            f"{sum(fails.values())} ({', '.join(f'{k} {n}' for k, n in fails.most_common())})"
            if fails
            else "0"
        )
        tokens = sum(r.tokens_in + r.tokens_out for r in rs)
        title = t.title.replace("|", "/")
        lines.append(
            f"| {t.key or f'#{t.id}'} {title} | {t.state.value} | {duration(wall) if rs else '—'} "
            f"| {per[roles.TESTER]} / {per[roles.BUILDER]} / {per[roles.REVIEWER]} "
            f"| {fail_text} | {tokens:,} |"
        )
    lines.append("")

    # -- stalls -------------------------------------------------------------------------
    if gaps:
        lines += ["## Stalls", "", "| From | To | Length | Meanwhile |", "|---|---|---|---|"]
        for s, e, why in gaps:
            lines.append(
                f"| {_when(s)} | {_when(e)} | {duration(e - s)} | {why.replace('|', '/')} |"
            )
        lines.append("")

    # -- the Reviewer and the app check -------------------------------------------------
    verdicts = [e for e in d.reviews if "passed" in e.data]
    notes = [e for e in d.reviews if e.data.get("note")]
    if verdicts or notes:
        rejected = sum(1 for e in verdicts if not e.data["passed"])
        lines += [
            "## Reviewer",
            "",
            f"- {len(verdicts)} reviews, {rejected} sent back to the Builder, {len(notes)} notes",
            "",
        ]
    apps = [r for r in d.tests if r.step == "app"]
    if apps:
        bad = sum(1 for r in apps if r.failed + r.errors)
        lines += ["## App check", "", f"- {len(apps)} checks, {bad} found problems", ""]
    return "\n".join(lines)


def save(project_id: int, now: float | None = None) -> Path:
    """Write the report under the reports directory; returns its path."""
    path = get_settings().reports_dir / f"project-{project_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(project_id, now))
    return path


def main(args: list[str]) -> None:
    if len(args) != 1 or not args[0].isdigit():
        raise SystemExit("usage: ai-factory report PROJECT_ID")
    from ai_factory.db import get_engine

    get_engine()
    try:
        path = save(int(args[0]))
    except KeyError:
        raise SystemExit(f"no project {args[0]}") from None
    print(path.read_text())
    print(f"(saved to {path})")
