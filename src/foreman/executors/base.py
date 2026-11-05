"""Executor interface: turns a task into a command plus control files.

Executors write whatever they need (prompt, tool config) into the run's
control directory, which is mounted read-only into the container at
$FOREMAN_CTL, and return the command to run in the task clone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from foreman.core.models import Project, Task


@dataclass(frozen=True)
class ExecutorContext:
    """What an executor may want to know about the model it runs against."""

    context_window: int | None = None
    max_output_tokens: int | None = None


class OutputParser(Protocol):
    """Turns an executor's raw stdout into what the dashboard shows, and spots a run
    that failed even though the process exited 0. One instance per run."""

    def render(self, line: str) -> str | None:
        """The text to show for one raw line, or None to show nothing."""
        ...

    def failure(self) -> str | None:
        """Why the run failed, if its output says so; checked after a 0 exit."""
        ...


@dataclass
class ExecutorInvocation:
    command: list[str]
    env: dict[str, str]
    # None: show output as-is and trust the exit status.
    output: OutputParser | None = field(default=None)


class Executor(Protocol):
    kind: str
    needs_node: bool
    # Tokens the agent itself puts in every model request (system prompt, tool schemas),
    # measured through the gateway. Comes off the window before the context budget.
    prompt_overhead_tokens: int

    def prepare(
        self,
        project: Project,
        task: Task,
        endpoint: str,
        model: str,
        ctl_dir: Path,
        prompt: str,
        context: ExecutorContext | None = None,
    ) -> ExecutorInvocation:
        """`endpoint` is the OpenAI-compatible base URL the executor must use (the
        run's model gateway URL); it carries no credentials."""
        ...
