"""Load eval tasks from `tasks/<id>/task.toml` and their fixture folders."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

TASKS_DIR = Path(__file__).parent / "tasks"

type Tag = Literal["general", "long-output", "edit", "mcp", "multi-read"]
type ToolName = Literal["read", "write", "edit", "bash", "grep", "find", "ls", "read_output"]


class TaskChecks(BaseModel):
    """Assertions on a run's events that a passing run must also satisfy.

    Attributes:
        no_rerun (str | None): A `bash` command containing this text may run only once.
        uses_tool (str | None): The run must call this tool at least once.
        max_tool_errors (int | None): The most failed tool calls a passing run may have.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    no_rerun: str | None = Field(default=None, min_length=1)
    uses_tool: str | None = Field(default=None, min_length=1)
    max_tool_errors: int | None = Field(default=None, ge=0)


class TaskDefinition(BaseModel):
    """The contents of one `task.toml`.

    Commands run with `bash -c` in the run's work folder. In them, `{python}` is
    replaced with the runner's Python interpreter and `{task}` with the task's
    folder, so verifier scripts can live outside the folder the model edits.

    Attributes:
        id (str): Unique task name; it must match the task's folder name.
        title (str): One line saying what the task asks for.
        tags (tuple[Tag, ...]): Areas the task measures, used to select tasks.
        prompt (str): What the user types to Wisp.
        setup (str | None): Runs after the fixture is copied and committed.
        verify (str): Runs after Wisp exits; exit status 0 means the task passed.
        checks (TaskChecks): Event assertions a passing run must also meet.
        timeout_seconds (float): Wisp's `--timeout` for the run.
        max_tool_iterations (int): Wisp's `--max-tool-iterations` for the run.
        tools (tuple[ToolName, ...]): The only tools Wisp exposes. Keep the list
            small: runs execute real commands on the host.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    title: str = Field(min_length=1)
    tags: tuple[Tag, ...] = Field(min_length=1)
    prompt: str = Field(min_length=1)
    setup: str | None = None
    verify: str = Field(min_length=1)
    checks: TaskChecks = TaskChecks()
    timeout_seconds: float = Field(default=300, gt=0)
    max_tool_iterations: int = Field(default=40, gt=0)
    tools: tuple[ToolName, ...] = Field(min_length=1)


class TaskError(ValueError):
    """A task folder is missing, malformed, or inconsistent."""


@dataclass(frozen=True, slots=True)
class Task:
    """A loaded task and the fixture its runs start from.

    Attributes:
        definition (TaskDefinition): The validated `task.toml`.
        fixture (Path): The `repo/` folder copied into each run's work folder.
    """

    definition: TaskDefinition
    fixture: Path

    @property
    def id(self) -> str:
        return self.definition.id

    @property
    def folder(self) -> Path:
        """The task's own folder, for verifier scripts the model cannot see."""

        return self.fixture.parent


def load_task(folder: Path) -> Task:
    """Load and validate one task folder.

    Args:
        folder (Path): A folder holding `task.toml` and a `repo/` fixture.

    Returns:
        Task: The validated task.

    Raises:
        TaskError: When `task.toml` or `repo/` is missing, the TOML is invalid,
            or the task's `id` differs from its folder name.
    """

    path = folder / "task.toml"
    fixture = folder / "repo"
    if not fixture.is_dir():
        raise TaskError(f"task {folder.name}: missing fixture folder {fixture}")
    try:
        with path.open("rb") as handle:
            definition = TaskDefinition.model_validate(tomllib.load(handle))
    except FileNotFoundError as err:
        raise TaskError(f"task {folder.name}: missing {path}") from err
    except (tomllib.TOMLDecodeError, ValidationError) as err:
        raise TaskError(f"task {folder.name}: invalid {path}: {err}") from err
    if definition.id != folder.name:
        raise TaskError(f"task {folder.name}: id {definition.id!r} must match its folder name")
    return Task(definition=definition, fixture=fixture)


def select_tasks(
    root: Path = TASKS_DIR,
    *,
    ids: Iterable[str] = (),
    tags: Iterable[str] = (),
) -> tuple[Task, ...]:
    """Load every task under ``root``, then keep those matching ``ids`` or ``tags``.

    Args:
        root (Path): Folder holding one subfolder per task.
        ids (Iterable[str]): Task ids to keep; empty keeps every id.
        tags (Iterable[str]): Tags to keep, any of which matches; empty keeps every tag.

    Returns:
        tuple[Task, ...]: The selected tasks, sorted by id.

    Raises:
        TaskError: When any task is invalid, or an id or tag names nothing.
    """

    tasks = tuple(load_task(folder) for folder in sorted(root.iterdir()) if folder.is_dir())
    wanted_ids = set(ids)
    wanted_tags = set(tags)
    unknown_ids = wanted_ids - {task.id for task in tasks}
    if unknown_ids:
        raise TaskError(f"no task named {', '.join(sorted(unknown_ids))}")
    unknown_tags = wanted_tags - {tag for task in tasks for tag in task.definition.tags}
    if unknown_tags:
        raise TaskError(f"no task tagged {', '.join(sorted(unknown_tags))}")
    return tuple(
        task
        for task in tasks
        if (not wanted_ids or task.id in wanted_ids)
        and (not wanted_tags or wanted_tags & set(task.definition.tags))
    )
