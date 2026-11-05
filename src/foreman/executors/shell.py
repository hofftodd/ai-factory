"""Runs the task's `executor_script` with bash. For scripted tasks and tests."""

from __future__ import annotations

from pathlib import Path

from foreman.core.models import Project, Task
from foreman.executors.base import ExecutorContext, ExecutorInvocation


class ShellExecutor:
    kind = "shell"
    prompt_overhead_tokens = 0
    needs_node = False

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
        (ctl_dir / "prompt.md").write_text(prompt)
        (ctl_dir / "script.sh").write_text(task.executor_script or "true\n")
        return ExecutorInvocation(
            command=["bash", "-c", 'exec bash "$FOREMAN_CTL/script.sh"'],
            env={},
        )
