from ai_factory.executors.aider import AiderExecutor
from ai_factory.executors.base import (
    EXIT_CONFIG,
    Executor,
    ExecutorContext,
    ExecutorInvocation,
    OutputParser,
)
from ai_factory.executors.opencode import OpenCodeExecutor
from ai_factory.executors.pi import PiExecutor
from ai_factory.executors.shell import ShellExecutor

EXECUTORS: dict[str, Executor] = {
    e.kind: e for e in (PiExecutor(), OpenCodeExecutor(), AiderExecutor(), ShellExecutor())
}


def get_executor(kind: str) -> Executor:
    try:
        return EXECUTORS[kind]
    except KeyError:
        raise ValueError(f"unknown executor {kind!r}; known: {sorted(EXECUTORS)}") from None


__all__ = [
    "EXECUTORS",
    "EXIT_CONFIG",
    "Executor",
    "ExecutorContext",
    "ExecutorInvocation",
    "OutputParser",
    "get_executor",
]
