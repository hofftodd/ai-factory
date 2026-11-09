"""Watchdog limits, failure signatures and the escalation ladder policy (FR-5).

Limits per task attempt (project ``watchdog`` setting overrides the defaults):

    max_requests     model calls an executor may make             (default 60)
    max_tokens       prompt + completion tokens across those calls (default 400k)
    max_diff_lines   lines added + removed by one attempt          (default 800)
    repeat_limit     identical model responses before tripping     (default 3)

Wall-clock time is the task's ``timeout_s``.

Escalation after a failed attempt (FR-5.5):

    retry (fresh agent session, told why the last attempt failed) x max_retries
      -> split into smaller tasks (planner)              if split is on
      -> retry on the escalation model                  if one is configured
      -> blocked: an inbox item asks the user

The same failure signature ``repeat_limit`` times in a row skips the remaining
retries at that level: the agent is going in circles.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ai_factory.core.models import Project

DEFAULTS: dict[str, Any] = {
    "escalation": True,  # off: a failed attempt just fails (the user decides)
    "max_requests": 60,
    "max_tokens": 400_000,
    "max_diff_lines": 800,
    "repeat_limit": 3,
    "max_retries": 2,
    "split": True,
    # Replanning after each completed task (FR-6).
    "replan": True,
    # Tasks the replanner may add, as a share of the approved plan (FR-6.3).
    "growth_cap_pct": 50,
    # Acceptance failures handed back to the replanner before asking the user.
    "max_acceptance_failures": 2,
}


def limit(project: Project, name: str) -> Any:
    return (project.watchdog or {}).get(name, DEFAULTS[name])


_VOLATILE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0x"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hash>"),
    (re.compile(r"\d+(\.\d+)?"), "N"),
    (re.compile(r"/tmp/\S+"), "<tmp>"),
    (re.compile(r"[ \t]+"), " "),
]


def failure_signature(text: str, tail_lines: int = 40) -> str:
    """Stable hash of a failure, ignoring numbers, hashes, temp paths and timing."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()][-tail_lines:]
    norm = "\n".join(lines)
    for pattern, repl in _VOLATILE:
        norm = pattern.sub(repl, norm)
    return hashlib.sha256(norm.encode()).hexdigest()[:16] if norm else ""


def response_signature(content: str, tool_calls: list[dict[str, Any]]) -> str:
    """Identity of a model response, for repeat detection."""
    calls = "|".join(
        f"{c.get('function', {}).get('name', '')}:{c.get('function', {}).get('arguments', '')}"
        for c in tool_calls
    )
    return hashlib.sha256(f"{content.strip()}\x00{calls}".encode()).hexdigest()[:16]


def repeated(signatures: list[str], n: int) -> bool:
    """True if the last n signatures are identical and non-empty."""
    tail = signatures[-n:]
    return len(tail) == n and bool(tail[0]) and len(set(tail)) == 1
