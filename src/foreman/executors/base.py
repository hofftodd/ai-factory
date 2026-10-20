"""Executor interface: turns a task into a command plus control files.

Executors write whatever they need (prompt, tool config) into the run's
control directory, which is mounted read-only into the container at
$FOREMAN_CTL, and return the command to run in the task clone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from foreman.core.models import Project, Task

# Exit status an executor's command uses for "this can't work as configured"
# (e.g. its tool isn't installed): the task blocks instead of being retried.
EXIT_CONFIG = 78


@dataclass
class ExecutorInvocation:
    command: list[str]
    env: dict[str, str]


class Executor(Protocol):
    kind: str
    needs_node: bool
    # Node providers whose API the executor's tool can speak (through the gateway).
    providers: frozenset[str]

    def prepare(
        self,
        project: Project,
        task: Task,
        endpoint: str,
        model: str,
        ctl_dir: Path,
        prompt: str,
        provider: str = "openai",
        role: str = "",
    ) -> ExecutorInvocation:
        """`endpoint` is the base URL the executor must use (the run's model gateway
        URL, ending in /v1); it carries no credentials. `provider` says which API it
        speaks: "openai" (chat completions) or "anthropic" (Claude Messages). `role` is
        the agent being run ("tester" / "builder"; "" for a single-step scripted task)."""
        ...
