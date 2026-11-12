"""Why an attempt failed, in the terms that matter after a run: was it the agent, or us?

The future-lab evaluation couldn't tell agent failures from factory bugs: 17 factory
PRs were merged while the run was going, and the git history shows only that tasks took
long. Each failed TaskRun has a `failure_kind` set where it failed; this maps it to a
cause, and the user can mark a blocked task's failure as a factory bug when resolving
it (`TaskRun.cause_override`).
"""

from __future__ import annotations

from ai_factory.core.models import TaskRun

AGENT, COORDINATION, FACTORY, SETUP, LIMIT, OTHER = (
    "agent",
    "coordination",
    "factory",
    "setup",
    "limit",
    "other",
)
CAUSES = (AGENT, COORDINATION, FACTORY, SETUP, LIMIT, OTHER)

CAUSE_OF_KIND: dict[str, str] = {
    # The agent's work wasn't good enough (or it gave up / ran away).
    "validation": AGENT,
    "no_red": AGENT,
    "no_tests": AGENT,
    "coverage": AGENT,
    "too_big": AGENT,
    "watchdog": AGENT,
    "fence": AGENT,
    "tests_conflict": AGENT,
    "review": AGENT,
    "app": AGENT,
    "executor": AGENT,
    # Parallel work colliding.
    "merge": COORDINATION,
    "behind": COORDINATION,
    "conflict": COORDINATION,
    # ai-factory itself crashed.
    "internal": FACTORY,
    # The project's configuration or toolchain.
    "config": SETUP,
    "image": SETUP,
    # A limit the user set.
    "budget": LIMIT,
}

DESCRIPTION = {
    AGENT: "the agent's work (failing tests, no tests, low coverage, review, app check...)",
    COORDINATION: "parallel tasks colliding (merge conflicts, work merged meanwhile)",
    FACTORY: "ai-factory itself (internal errors, or marked a factory bug by you)",
    SETUP: "the project's configuration or toolchain",
    LIMIT: "a limit you set (the spend cap)",
    OTHER: "not classified",
}


def cause_of(run: TaskRun) -> str:
    """The cause of a failed run: the user's verdict if they gave one, else its kind's."""
    if run.cause_override in CAUSES:
        return run.cause_override
    return CAUSE_OF_KIND.get(run.failure_kind, OTHER)
