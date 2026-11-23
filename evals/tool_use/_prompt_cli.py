"""Launch the selected Wisp CLI with prompt overrides confined to the eval subprocess."""

from __future__ import annotations

import json
import runpy
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from wisp.coding import session
from wisp.coding.prompt import builder
from wisp.coding.prompt.builder import assemble_prompt
from wisp.coding.prompt.parts import AssembledPrompt, PromptPart


def main() -> None:
    """Read the runner's private prompt options and execute its console script."""

    options_path, executable, *arguments = sys.argv[1:]
    options: dict[str, str | None] = json.loads(Path(options_path).read_text(encoding="utf-8"))
    project_guidance = options["project_guidance"]

    def assemble_with_guidance(
        *,
        additional_guidance: Sequence[PromptPart] = (),
        include_project_context: bool = True,
        **kwargs: Any,
    ) -> AssembledPrompt:
        if include_project_context and project_guidance is not None:
            additional_guidance = (
                *additional_guidance,
                PromptPart(
                    "project_instructions",
                    "experimental guidance",
                    f"[WISP EVAL PROJECT GUIDANCE]\n{project_guidance}",
                ),
            )
        return assemble_prompt(
            additional_guidance=additional_guidance,
            include_project_context=include_project_context,
            **kwargs,
        )

    with ExitStack() as stack:
        if options["core_prompt"] is not None:
            stack.enter_context(
                patch.object(builder, "DEFAULT_SYSTEM_PROMPT", options["core_prompt"])
            )
        if project_guidance is not None:
            stack.enter_context(patch.object(session, "assemble_prompt", assemble_with_guidance))
        sys.argv = [executable, *arguments]
        runpy.run_path(executable, run_name="__main__")


if __name__ == "__main__":
    main()
