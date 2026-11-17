"""Declarative, bounded RPC scenarios for tool-use evaluations."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from typing_extensions import TypeAliasType


class ScenarioModel(BaseModel):
    """Immutable eval configuration rejecting unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ApprovalDecision(ScenarioModel):
    """Expected approval request; grants default to once and denials have no scope."""

    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    # Each variant is a complete exact argument object, never a partial match.
    alternative_arguments: tuple[dict[str, JsonValue], ...] = ()
    approved: bool
    scope: Literal["once", "tool_session"] | None = None

    @model_validator(mode="after")
    def approval_scope(self) -> Self:
        if not self.approved and self.scope is not None:
            raise ValueError("denied approvals cannot specify a scope")
        if self.approved and self.scope is None:
            object.__setattr__(self, "scope", "once")
        return self


class PromptStep(ScenarioModel):
    """Run one prompt with ordered approvals and optional assistant-text assertion."""

    type: Literal["prompt"] = "prompt"
    prompt: str = Field(min_length=1)
    approvals: tuple[ApprovalDecision, ...] = ()
    expect_text: str | None = None


class CheckpointStep(ScenarioModel):
    """Remember the current persisted session and active leaf under a unique name."""

    type: Literal["checkpoint"] = "checkpoint"
    name: str = Field(min_length=1)


class NavigateStep(ScenarioModel):
    """Restore a checkpoint declared earlier in the same scenario."""

    type: Literal["navigate"] = "navigate"
    checkpoint: str = Field(min_length=1)


class ResumeStep(ScenarioModel):
    """Restart the backend and restore the selected session and active leaf."""

    type: Literal["resume"] = "resume"


class CompactStep(ScenarioModel):
    """Require a successful manual compaction before continuing."""

    type: Literal["compact"] = "compact"
    instructions: str | None = None


Step = TypeAliasType(
    "Step",
    Annotated[
        PromptStep | CheckpointStep | NavigateStep | ResumeStep | CompactStep,
        Field(discriminator="type"),
    ],
)


class Scenario(ScenarioModel):
    """Ordered RPC steps with a prompt and valid earlier checkpoint references."""

    steps: tuple[Step, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def checkpoint_references(self) -> Self:
        checkpoints: set[str] = set()
        has_prompt = False
        for step in self.steps:
            if isinstance(step, PromptStep):
                has_prompt = True
            elif isinstance(step, CheckpointStep):
                if step.name in checkpoints:
                    raise ValueError(f"duplicate checkpoint {step.name!r}")
                checkpoints.add(step.name)
            elif isinstance(step, NavigateStep) and step.checkpoint not in checkpoints:
                raise ValueError(f"unknown earlier checkpoint {step.checkpoint!r}")
        if not has_prompt:
            raise ValueError("scenario requires at least one prompt")
        return self


class ScenarioResult(ScenarioModel):
    """Completed step count, task assertions, infrastructure error, and deadline outcome."""

    completed_steps: int = Field(ge=0)
    failed_checks: tuple[str, ...] = ()
    error: str | None = None
    timed_out: bool = False
