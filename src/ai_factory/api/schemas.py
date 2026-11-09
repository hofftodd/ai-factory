"""Request bodies. Responses are the table models themselves."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ModelChoice(BaseModel):
    node_id: int
    model: str = ""


class RoleBinding(BaseModel):
    node_id: int | None = None
    model: str = ""
    # Further (node, model) choices, in order of preference: work goes to the first one with
    # a free slot (see roles.choices). node_id/model above is the first choice.
    also: list[ModelChoice] = Field(default_factory=list, max_length=16)
    # Coding agents only (Tester, Builder, escalation): passed to the model's chat template
    # as `reasoning_effort` (e.g. low, medium, high, xhigh); "" leaves the model's default.
    reasoning_effort: str = Field(default="", pattern=r"^[a-z]{0,16}$")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    repo_url: str = Field(min_length=1)
    milestone_branch: str = "ai-factory/m1"
    feature: str = Field(default="001-initial", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    # Skip Phase 1 and go straight to manually added tasks.
    skip_spec: bool = False
    executor_kind: str = "pi"
    network: Literal["isolated", "open"] = "isolated"
    role_bindings: dict[str, RoleBinding] = Field(default_factory=dict)
    push_after_merge: bool = False
    gates: dict[str, Any] = Field(default_factory=dict)
    watchdog: dict[str, Any] = Field(default_factory=dict)
    spend_cap_usd: float = Field(default=0.0, ge=0)
    min_task_coverage: int = Field(default=80, ge=0, le=100)
    # How the project's tests already run (see Project.test_note).
    test_note: str = Field(default="", max_length=2000)


class ProjectUpdate(BaseModel):
    name: str | None = None
    acceptance_cmd: str | None = None
    max_task_files: int | None = Field(default=None, ge=1, le=20)
    context_budget_pct: int | None = Field(default=None, ge=5, le=90)
    executor_kind: str | None = None
    network: Literal["isolated", "open"] | None = None
    role_bindings: dict[str, RoleBinding] | None = None
    gates: dict[str, Any] | None = None
    watchdog: dict[str, Any] | None = None
    push_after_merge: bool | None = None
    spend_cap_usd: float | None = Field(default=None, ge=0)
    min_task_coverage: int | None = Field(default=None, ge=0, le=100)
    test_paths: list[str] | None = None
    test_note: str | None = Field(default=None, max_length=2000)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1)
    # The goal, in plain language.
    description: str = ""
    # Plain-language acceptance criteria; the Tester turns them into tests.
    criteria: list[str] = Field(default_factory=list)
    todos: list[str] = Field(default_factory=list)
    # Scripted (shell executor) tasks only: ai-factory's own tests and demos.
    allowed_globs: list[str] = Field(default_factory=list)
    validation_cmd: str = ""
    deps: list[int] = Field(default_factory=list)
    executor_kind: str = ""
    node_id: int | None = None
    model: str = ""
    executor_script: str = ""
    timeout_s: int = Field(default=1800, ge=10, le=24 * 3600)


class TaskUpdate(BaseModel):
    title: str | None = None
    criteria: list[str] | None = None
    req_refs: list[str] | None = None
    context_files: list[str] | None = None
    description: str | None = None
    todos: list[str] | None = None
    allowed_globs: list[str] | None = None
    validation_cmd: str | None = None
    deps: list[int] | None = None
    executor_kind: str | None = None
    node_id: int | None = None
    model: str | None = None
    executor_script: str | None = None
    timeout_s: int | None = None


class NodeCreate(BaseModel):
    name: str = Field(min_length=1)
    provider: Literal["openai", "anthropic"] = "openai"
    base_url: str = Field(min_length=1)
    context_window: int = Field(default=32768, ge=1024, le=10_000_000)
    max_output_tokens: int = Field(default=0, ge=0, le=1_000_000)
    api_key: str = ""
    capacity: int = Field(default=1, ge=1, le=64)
    models: list[str] = Field(default_factory=list)
    price_in: float = Field(default=0.0, ge=0)
    price_out: float = Field(default=0.0, ge=0)


class NodeUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    context_window: int | None = Field(default=None, ge=1024, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=0, le=1_000_000)
    api_key: str | None = None
    capacity: int | None = Field(default=None, ge=1, le=64)
    price_in: float | None = Field(default=None, ge=0)
    price_out: float | None = Field(default=None, ge=0)


class GitHubRepoCreate(BaseModel):
    owner: str = Field(min_length=1, pattern=r"^[A-Za-z0-9-]+$")
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    private: bool = True
    description: str = ""


class GitHubTokenUpdate(BaseModel):
    token: str = Field(min_length=1)


class ControlRequest(BaseModel):
    scope: Literal["global", "project", "task"]
    target_id: int | None = None
    action: Literal["pause", "resume", "stop", "estop", "clear_estop"]


class RetryRequest(BaseModel):
    reset: bool = False  # discard the task clone and start from the milestone tip


class SendBackRequest(BaseModel):
    note: str = ""  # what the Tester should change; shown in its briefing


class DocUpdate(BaseModel):
    content: str


class EnvironmentRebuild(BaseModel):
    # What's missing or should change, for the Environment Agent (optional).
    note: str = ""


class ChatMessage(BaseModel):
    content: str = Field(min_length=1)
