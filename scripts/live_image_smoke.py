"""Check live that every provider accepts the image formats Wisp sends.

This is opt-in and not part of CI: it calls real provider APIs with the credentials
Wisp already has (stored with ``wisp auth`` or set in the provider's environment
variable) and costs a few cents per target. A target without credentials is skipped.

For each target it runs three prompts through ``CodingSession``:

1. ``prompt``: a user message with an attached image.
2. ``tool``: the model reads an image file with the ``read`` tool.
3. ``resume``: a second run in the same session asks about that image again without
   reading it. Providers that replay tool calls natively should resend the image;
   the others name it in a summary, so they may re-read it or say they cannot see it.

Each check passes when the API accepts the request and the model names the expected
colour. For text-only models a check passes when the request succeeds without the
image, since Wisp sends placeholders instead.

Run from the repository root::

    uv run python scripts/live_image_smoke.py
    uv run python scripts/live_image_smoke.py --only google anthropic:claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
import zlib
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import anyio

from wisp.auth.connections import API_KEY_ENVIRONMENT_VARIABLES
from wisp.auth.storage import JsonAuthStore
from wisp.coding.session import CodingSession, ImageInputUnsupportedError
from wisp.config.runtime import default_auth_path
from wisp.events import ToolResultReady, WispEvent
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.base import (
    Provider,
    model_accepts_images,
    structured_tool_replacement_support,
)
from wisp.providers.catalog import ModelRegistry, effective_catalog
from wisp.providers.events import ProviderEvent
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.builtin import ReadTool
from wisp.tools.context import ToolContext

Colour = tuple[int, int, int]
COLOURS: dict[str, Colour] = {
    "red": (230, 20, 20),
    "blue": (20, 40, 230),
    "green": (20, 180, 40),
    "yellow": (245, 215, 0),
}


def two_colour_png(left: str, right: str, *, size: int = 96) -> bytes:
    """Return a PNG whose left half is one solid colour and right half another."""

    row = b"\x00" + bytes(COLOURS[left]) * (size // 2) + bytes(COLOURS[right]) * (size // 2)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


@dataclass(frozen=True)
class Target:
    label: str
    credential: str
    model: str
    build: Callable[[StoredProviderAuthResolver], Provider]
    # An OpenAI-compatible endpoint has no catalog entry, so its adapter's own
    # image support decides, as it does for a user-configured endpoint.
    use_catalog: bool = True


class _AliasAuth:
    """Resolve another provider's credential, for an OpenAI-compatible target.

    The compatible adapter looks up only its own environment variables, so the
    other provider's variables are checked here before its stored credential.
    """

    def __init__(self, inner: StoredProviderAuthResolver, provider: str) -> None:
        self._inner = inner
        self._provider = provider

    async def api_key(self, provider: str) -> str | None:
        del provider
        for name in API_KEY_ENVIRONMENT_VARIABLES.get(self._provider, ()):
            if key := os.environ.get(name):
                return key
        return await self._inner.api_key(self._provider)

    async def bearer_token(self, provider: str, **kwargs: object) -> object:
        del provider
        return await self._inner.bearer_token(self._provider, **kwargs)  # type: ignore[arg-type]


def _targets() -> list[Target]:
    from wisp.providers.anthropic import AnthropicProvider
    from wisp.providers.deepseek import DeepSeekProvider
    from wisp.providers.google import GoogleProvider
    from wisp.providers.openai import OpenAIProvider
    from wisp.providers.openai_codex import OpenAICodexProvider
    from wisp.providers.openai_compatible import OpenAICompatibleProvider
    from wisp.providers.xai import XAIProvider

    def xai_chat(auth: StoredProviderAuthResolver) -> Provider:
        # xAI also serves Chat Completions, which exercises the OpenAI-compatible
        # adapter's image path with a vision model.
        return OpenAICompatibleProvider(
            base_url="https://api.x.ai/v1",
            default_model="grok-4.6",
            provider_name="xai-chat",
            auth_resolver=_AliasAuth(auth, "xai"),  # type: ignore[arg-type]
        )

    return [
        Target("openai", "openai", "gpt-5.6-sol", lambda a: OpenAIProvider(auth_resolver=a)),
        Target(
            "openai-codex",
            "openai-codex",
            "gpt-5.6-sol",
            lambda a: OpenAICodexProvider(auth_resolver=a),
        ),
        Target("xai", "xai", "grok-4.6", lambda a: XAIProvider(auth_resolver=a)),
        Target("xai-chat", "xai", "grok-4.6", xai_chat, use_catalog=False),
        Target(
            "anthropic",
            "anthropic",
            "claude-sonnet-5",
            lambda a: AnthropicProvider(auth_resolver=a),
        ),
        Target(
            "google:gemini-3.6-flash",
            "google",
            "gemini-3.6-flash",
            lambda a: GoogleProvider(auth_resolver=a),
        ),
        Target(
            "google:gemini-2.5-flash",
            "google",
            "gemini-2.5-flash",
            lambda a: GoogleProvider(auth_resolver=a),
        ),
        Target(
            "deepseek",
            "deepseek",
            "deepseek-v4-pro",
            lambda a: DeepSeekProvider(auth_resolver=a),
        ),
    ]


class _RecordingProvider:
    """Forward to a provider and record which requests carried image bytes."""

    def __init__(self, inner: Provider) -> None:
        self._inner = inner
        self.requests: list[tuple[bool, bool]] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def stream(self, messages: Sequence[object], **kwargs: object) -> AsyncIterator[ProviderEvent]:
        history_images = any(getattr(m, "image_bytes", None) for m in messages)
        tool_images = any(r.image_bytes for r in kwargs.get("tool_results", ()))  # type: ignore[attr-defined]
        self.requests.append((history_images, tool_images))
        return self._inner.stream(messages, **kwargs)  # type: ignore[arg-type]


@dataclass
class Check:
    name: str
    status: str
    detail: str


async def _run(
    agent: CodingSession, prompt: str, session: object, images: Sequence[bytes] = ()
) -> tuple[str, list[WispEvent]]:
    events = [event async for event in agent.run(prompt, images=images, session=session)]  # type: ignore[arg-type]
    errors = [str(getattr(event, "message", "")) for event in events if event.type == "error"]
    if errors:
        # A failed run still completes normally, so its error is the result.
        raise RuntimeError(errors[-1])
    replies = [m for m in session.read_messages() if m.role == "assistant" and m.content]  # type: ignore[attr-defined]
    return (replies[-1].content if replies else ""), events


def _verdict(answer: str, expected: str, *, sees_images: bool) -> str:
    if not sees_images:
        return "pass"
    return "pass" if expected in answer.lower() else "wrong-answer"


async def smoke(target: Target, auth: StoredProviderAuthResolver, workdir: Path) -> list[Check]:
    provider = _RecordingProvider(target.build(auth))
    models = ModelRegistry(effective_catalog()) if target.use_catalog else None
    sees_images = model_accepts_images(provider, target.model, models)  # type: ignore[arg-type]
    # Only adapters with native tool history resend an earlier tool image; the
    # others replay old tool calls as a summary that names it.
    replays_tool_history = (
        structured_tool_replacement_support(provider, effort=None) is True  # type: ignore[arg-type]
    )
    (workdir / "swatch.png").write_bytes(two_colour_png("green", "yellow"))
    tools = ToolRegistry()
    tools.register(ReadTool())
    store = JsonlSessionStore(workdir / "sessions")
    agent = CodingSession(
        provider=provider,  # type: ignore[arg-type]
        sessions=store,
        model=target.model,
        models=models,
        tool_registry=tools,
        tool_context=ToolContext(cwd=workdir),
        max_tool_iterations=4,
    )
    checks: list[Check] = []

    async def attempt(name: str, step: Callable[[], object]) -> None:
        try:
            checks.append(await step())  # type: ignore[misc]
        except ImageInputUnsupportedError as exc:
            status = "pass" if not sees_images else "error"
            checks.append(Check(name, status, f"refused before sending: {exc}"))
        except Exception as exc:  # noqa: BLE001 - every provider failure is a result here
            checks.append(Check(name, "error", f"{type(exc).__name__}: {exc}"[:600]))

    async def prompt_check() -> Check:
        answer, _ = await _run(
            agent,
            "What colour is the left half of the attached image? Answer with one word.",
            store.create(),
            [two_colour_png("red", "blue")],
        )
        return Check("prompt", _verdict(answer, "red", sees_images=sees_images), answer[:200])

    tool_session = store.create()

    async def tool_check() -> Check:
        answer, events = await _run(
            agent,
            "Use the read tool to open swatch.png, then tell me the colour of its left "
            "half. Answer with one word.",
            tool_session,
        )
        returned = [e for e in events if isinstance(e, ToolResultReady) and e.images]
        if not returned:
            return Check("tool", "no-tool-image", answer[:200])
        sent = any(tool for _, tool in provider.requests)
        detail = f"{answer[:200]} (tool image bytes sent: {sent})"
        return Check("tool", _verdict(answer, "green", sees_images=sees_images), detail)

    async def resume_check() -> Check:
        before = len(provider.requests)
        answer, events = await _run(
            agent,
            "Without reading any file again, what colour is the right half of the "
            "swatch.png image you read earlier? Answer with one word, or say unknown.",
            tool_session,
        )
        reread = any(isinstance(e, ToolResultReady) for e in events)
        replayed = any(history for history, _ in provider.requests[before:])
        detail = f"{answer[:200]} (image replayed: {replayed}, re-read: {reread})"
        if not sees_images:
            return Check("resume", "pass", detail)
        if "yellow" in answer.lower():
            return Check("resume", "pass", detail)
        if replays_tool_history:
            return Check("resume", "wrong-answer" if replayed else "no-replay", detail)
        # A provider that summarizes earlier tool calls cannot see the image again.
        return Check("resume", "pass-no-replay", detail)

    await attempt("prompt", prompt_check)
    await attempt("tool", tool_check)
    await attempt("resume", resume_check)
    return checks


async def _has_credentials(target: Target, auth: StoredProviderAuthResolver) -> bool:
    if any(
        os.environ.get(name) for name in API_KEY_ENVIRONMENT_VARIABLES.get(target.credential, ())
    ):
        return True
    return auth.store.get(target.credential) is not None


async def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Check live that providers accept images.")
    parser.add_argument("--only", nargs="*", help="target labels or label prefixes to run")
    args = parser.parse_args(argv)
    auth = StoredProviderAuthResolver(JsonAuthStore(default_auth_path()))
    targets = [
        t
        for t in _targets()
        if not args.only or any(t.label == o or t.label.startswith(f"{o}:") for o in args.only)
    ]
    print(f"Live image smoke, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    failed = False
    for target in targets:
        print(f"\n## {target.label} ({target.model})")
        if not await _has_credentials(target, auth):
            print("  skipped: no credentials")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            for check in await smoke(target, auth, Path(tmp)):
                failed |= check.status not in {"pass", "pass-no-replay"}
                print(f"  {check.name:7s} {check.status:15s} {check.detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(anyio.run(main, sys.argv[1:]))
