"""ai-swap leases: keep a step's model loaded from the step's start to its end.

ai-swap protects a model only while a request is in flight; a coding agent pauses between
requests while its tools run, so another client's load could evict its model mid-step. Each
Tester/Builder step (and the single-step executor) and each planner job on an ai-swap node
holds a lease on its model (``ai_common.swap.lease``) for as long as it runs: taken when it
starts, renewed every third of ``AI_FACTORY_LEASE_TTL_S`` (default 10 min), released when it
ends, however it ends (done, failed, stopped, cancelled at shutdown).

The lease id is recorded on the TaskRun or PlannerJob, so the restart recovery releases
leases a crashed orchestrator left behind instead of waiting for them to lapse.

Residency (``ai_factory.residency``) still decides where and when work starts; a lease only
keeps the model in place once it has. Nodes that aren't ai-swap endpoints get no lease, and
an ai-swap that refuses one (or is too old to have the API) doesn't stop the step.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable

import httpx
from ai_common import swap

from ai_factory import residency
from ai_factory.config import get_settings
from ai_factory.core.models import Node

log = logging.getLogger("ai_factory.leases")


def owner(project: str, what: str) -> str:
    """The lease's owner as ai-swap shows it: ``ai-factory/<project>/<task or planner>``."""
    return f"ai-factory/{project}/{what}"


@contextlib.asynccontextmanager
async def hold(
    node: Node | None,
    model: str,
    owner: str,
    reason: str = "",
    record: Callable[[str], None] | None = None,
) -> AsyncIterator[swap.Lease | None]:
    """Hold a lease on ``model`` for the body when ``node`` is an ai-swap endpoint (its
    ``/api/status`` answered); yields the lease, or None when there is none. ``record`` is
    called with the lease id once taken and with "" once released."""
    base = residency.root(node) if node is not None else ""
    if node is None or not model or residency.view_for(node) is None:
        yield None
        return
    async with contextlib.AsyncExitStack() as stack:
        try:
            held: swap.Lease | None = await stack.enter_async_context(
                swap.lease(
                    base,
                    model,
                    owner,
                    get_settings().lease_ttl_s,
                    reason=reason,
                    api_key=residency.api_key(node),
                )
            )
        except (swap.SwapError, httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("no lease on %s at %s for %s: %s", model, base, owner, e)
            held = None
        if held is not None and held.held and record is not None:
            record(held.id)
            stack.callback(record, "")
        yield held


def release(node: Node | None, model: str, lease_id: str) -> None:
    """Release a lease left by an earlier orchestrator (restart recovery). Best effort:
    a lease that can't be released lapses within its TTL."""
    if node is None or not lease_id:
        return
    base = residency.root(node)
    try:
        httpx.delete(
            f"{base}/api/leases/{lease_id}",
            params={"model": model},
            headers=residency.headers(node),
            timeout=5,
        )
    except httpx.HTTPError as e:
        log.warning("releasing lease %s on %s at %s: %s", lease_id, model, base, e)
