"""Model residency: don't make a model server swap models out from under running work.

With ai-swap (https://github.com/hofftodd/ai-swap) in front of the model servers, several
models share one machine's memory and are loaded on demand. A coding agent's step sends many
requests with gaps between them (while its tools run). ai-swap only protects a model while a
request is in flight, so two steps on one machine whose models don't fit together would swap
each other out on almost every turn: about 25 s each time on a 32 GB GPU, minutes for a
150 GB model. ai-factory knows that a step uses its model from start to finish, so it decides:

- work whose model is loaded, or fits in the machine's free memory, starts as usual;
- work whose model would push out a model that a running ai-factory step or planner call is
  using (or one serving a request right now) waits, with one event saying why;
- among work that can start, work whose model is already loaded goes first;
- after ``AI_FACTORY_SWAP_WAIT_S`` of waiting (default 30 min) work starts anyway, so nothing
  waits forever.

Which models ai-swap would push out is worked out the way ai-swap does it: idle models,
least recently used first, until the new one fits. Nodes that aren't ai-swap endpoints (plain
llama.cpp, LM Studio, Claude) answer no ``/api/status`` and are left alone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlmodel import select

from ai_factory.core.models import JobState, Node, PlannerJob, Project, RunState, TaskRun
from ai_factory.db import session

log = logging.getLogger("ai_factory.residency")

ACTIVE = ("ready", "starting", "stopping")  # states that hold memory in ai-swap
REFRESH_S = 3.0
NOT_SWAP_RECHECK_S = 60.0


@dataclass
class ModelState:
    name: str
    state: str
    memory_gb: float
    inflight: int = 0
    last_used: str = ""


@dataclass
class Machine:
    name: str
    budget_gb: float
    models: dict[str, ModelState] = field(default_factory=dict)
    # Set while someone (e.g. an ai-bench run) has reserved the machine: "owner (reason)".
    reserved: str = ""


@dataclass
class View:
    """One ai-swap endpoint's picture of the cluster."""

    machines: dict[str, Machine]
    ids: dict[str, tuple[str, str]]  # model id clients use -> (machine, model name there)
    fetched: float


@dataclass
class Placement:
    machine: Machine
    model: ModelState

    @property
    def loaded(self) -> bool:
        return self.model.state in ("ready", "starting")


# Endpoint root ("http://studio.local:8080") -> View, or the time it was found not to be ai-swap.
_views: dict[str, View | float] = {}
_waiting: dict[tuple[str, int], float] = {}  # ("task"|"job", id) -> when it started waiting


def reset() -> None:
    """Forget cached views and waits (tests)."""
    _views.clear()
    _waiting.clear()


def root(node: Node) -> str:
    u = urlsplit(node.base_url)
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""


def _parse(status: dict[str, Any], models: dict[str, Any]) -> View:
    machines: dict[str, Machine] = {}
    nodes = [status.get("node") or {}] + [
        p.get("node") or {} for p in status.get("peers") or [] if p.get("online")
    ]
    for n in nodes:
        if not n.get("name"):
            continue
        m = Machine(n["name"], float(n.get("memory_budget_gb") or 0))
        if lock := n.get("lock"):
            m.reserved = lock.get("owner", "someone") + (
                f" ({lock['reason']})" if lock.get("reason") else ""
            )
        for x in n.get("models") or []:
            m.models[x["name"]] = ModelState(
                x["name"],
                x.get("state", ""),
                float(x.get("memory_gb") or 0),
                int(x.get("inflight") or 0),
                x.get("last_used") or "",
            )
        machines[m.name] = m
    ids: dict[str, tuple[str, str]] = {}
    for e in models.get("data") or []:
        mid, machine = e.get("id"), e.get("node")
        if mid and machine:
            ids[mid] = (machine, mid.split("@", 1)[0])
    return View(machines, ids, time.monotonic())


async def fetch(node: Node, client: httpx.AsyncClient) -> View | None:
    """The cluster as seen through this node, or None if it isn't an ai-swap endpoint."""
    base = root(node)
    if node.provider != "openai" or not base:
        return None
    try:
        st, md = await asyncio.gather(
            client.get(base + "/api/status"), client.get(base + "/v1/models")
        )
        if st.status_code != 200 or md.status_code != 200:
            return None
        status = st.json()
        if not isinstance(status, dict) or "node" not in status:
            return None
        return _parse(status, md.json())
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


async def refresh(nodes: list[Node] | None = None) -> None:
    """Re-read every ai-swap node (the orchestrator calls this every few seconds)."""
    if nodes is None:
        with session() as s:
            nodes = list(s.exec(select(Node)).all())
    now = time.monotonic()
    todo: dict[str, Node] = {}
    for n in nodes:
        base = root(n)
        cached = _views.get(base)
        if isinstance(cached, float) and now - cached < NOT_SWAP_RECHECK_S:
            continue  # recently found not to be ai-swap
        todo.setdefault(base, n)
    async with httpx.AsyncClient(timeout=3) as client:
        views = await asyncio.gather(*(fetch(n, client) for n in todo.values()))
    for base, v in zip(todo, views, strict=True):
        _views[base] = v if v is not None else now


def due() -> bool:
    fresh = [v.fetched for v in _views.values() if isinstance(v, View)]
    return not fresh or time.monotonic() - min(fresh) > REFRESH_S


def view_for(node: Node | None, views: dict[str, View | float] | None = None) -> View | None:
    if node is None:
        return None
    v = (_views if views is None else views).get(root(node))
    return v if isinstance(v, View) else None


def placement(
    node: Node | None, model: str, views: dict[str, View | float] | None = None
) -> Placement | None:
    v = view_for(node, views)
    if v is None or model not in v.ids:
        return None
    machine, name = v.ids[model]
    m = v.machines.get(machine)
    if m is None or name not in m.models:
        return None
    return Placement(m, m.models[name])


# -- who is using what ----------------------------------------------------------------


def in_use() -> dict[tuple[str, str], str]:
    """(machine, model) -> who in ai-factory is using it right now: running task steps and
    planner calls on ai-swap nodes."""
    with session() as s:
        runs = s.exec(select(TaskRun).where(TaskRun.state == RunState.RUNNING)).all()
        jobs = s.exec(select(PlannerJob).where(PlannerJob.state == JobState.RUNNING)).all()
        nodes = {n.id: n for n in s.exec(select(Node))}
        projects = {p.id: p.name for p in s.exec(select(Project))}
    out: dict[tuple[str, str], str] = {}
    for r in runs:
        p = placement(nodes.get(r.node_id), r.model) if r.node_id is not None else None
        if p is not None:
            who = f"{projects.get(r.project_id, r.project_id)}'s {r.step or 'agent'}"
            out.setdefault((p.machine.name, p.model.name), who)
    for j in jobs:
        p = placement(nodes.get(j.node_id), j.model) if j.node_id is not None else None
        if p is not None:
            who = f"{projects.get(j.project_id, j.project_id)}'s planner"
            out.setdefault((p.machine.name, p.model.name), who)
    return out


# -- the decision -----------------------------------------------------------------------


def would_evict(
    p: Placement, planned: AbstractSet[str] = frozenset()
) -> tuple[list[ModelState], bool]:
    """What ai-swap would unload to load ``p.model``: idle models, least recently used
    first, until it fits. The flag says whether that is enough (if not, ai-swap waits for
    busy models). ``planned`` are models this pass has already decided to start."""
    m = p.machine
    if p.loaded or p.model.name in planned or not m.budget_gb:
        return [], True
    held = [x for x in m.models.values() if x.state in ACTIVE or x.name in planned]
    need = sum(x.memory_gb for x in held) + p.model.memory_gb - m.budget_gb
    if need <= 0:
        return [], True
    idle = sorted(
        (x for x in held if x.state == "ready" and not x.inflight and x.name not in planned),
        key=lambda x: x.last_used,
    )
    evicted = []
    for x in idle:
        if need <= 0:
            break
        evicted.append(x)
        need -= x.memory_gb
    return evicted, need <= 0


def blocker(
    node: Node | None,
    model: str,
    busy: dict[tuple[str, str], str],
    planned: dict[str, set[str]] | None = None,
) -> str:
    """Why starting work on (node, model) now would swap out a model in use; "" if it's fine."""
    p = placement(node, model)
    if p is None:
        return ""
    if p.machine.reserved:
        return f"{p.machine.name} is reserved by {p.machine.reserved}"
    mine = (planned or {}).get(p.machine.name, set())
    evicted, fits = would_evict(p, mine)
    hit = [x for x in evicted if (p.machine.name, x.name) in busy]
    if hit:
        users = ", ".join(f"{x.name} ({busy[(p.machine.name, x.name)]})" for x in hit)
        return f"loading {model} on {p.machine.name} would swap out {users}"
    if not fits:
        return f"{p.machine.name} has no room for {model} until a model in use there finishes"
    return ""


def warm(node: Node | None, model: str) -> bool:
    """The model is loaded (or loading) on its machine: no swap needed."""
    p = placement(node, model)
    return p is None or (p.loaded and not p.machine.reserved)


def reserve(planned: dict[str, set[str]], node: Node | None, model: str) -> None:
    p = placement(node, model)
    if p is not None:
        planned.setdefault(p.machine.name, set()).add(p.model.name)


def hold(kind: str, ident: int, reason: str, max_wait_s: float) -> tuple[bool, bool]:
    """(keep waiting, first time) for work held back for ``reason`` ("" = not held).
    The first call starts the clock; past ``max_wait_s`` the work stops waiting."""
    key = (kind, ident)
    if not reason:
        _waiting.pop(key, None)
        return False, False
    first = key not in _waiting
    started = _waiting.setdefault(key, time.monotonic())
    if max_wait_s and time.monotonic() - started > max_wait_s:
        log.warning("%s %s waited %.0f s; starting despite: %s", kind, ident, max_wait_s, reason)
        _waiting.pop(key, None)
        return False, False
    return True, first


# -- bindings that can't share a machine --------------------------------------------------


async def binding_conflicts(bindings: dict[str, dict[str, Any]]) -> list[str]:
    """Warnings for role bindings whose models share a machine but don't fit in it together:
    every hand-over between those agents would swap models."""
    with session() as s:
        nodes = {n.id: n for n in s.exec(select(Node))}
    wanted = {r: b for r, b in bindings.items() if b and b.get("node_id") and b.get("model")}
    endpoints = {
        root(nodes[b["node_id"]]): nodes[b["node_id"]]
        for b in wanted.values()
        if b["node_id"] in nodes
    }
    views: dict[str, View | float] = {}
    async with httpx.AsyncClient(timeout=3) as client:
        for n in endpoints.values():
            v = await fetch(n, client)
            if v is not None:
                views[root(n)] = v
    per_machine: dict[str, dict[str, tuple[float, list[str]]]] = {}
    budgets: dict[str, float] = {}
    for role, b in wanted.items():
        p = placement(nodes.get(b["node_id"]), b["model"], views)
        if p is None:
            continue
        budgets[p.machine.name] = p.machine.budget_gb
        mem, rs = per_machine.setdefault(p.machine.name, {}).get(
            p.model.name, (p.model.memory_gb, [])
        )
        per_machine[p.machine.name][p.model.name] = (mem, [*rs, role.replace("_", " ")])
    out = []
    for machine, models in per_machine.items():
        total = sum(mem for mem, _ in models.values())
        if len(models) > 1 and budgets[machine] and total > budgets[machine]:
            parts = "; ".join(
                f"{', '.join(rs)}: {name} ({mem:g} GB)" for name, (mem, rs) in models.items()
            )
            out.append(
                f"On {machine} these don't fit in {budgets[machine]:g} GB together ({parts}), "
                "so handing work between them swaps models each time."
            )
    return out
