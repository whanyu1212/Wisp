"""Measure full-context estimation and fingerprinting costs."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmarks.support import Measurement, environment, measure
from wisp.agent.context import context_fingerprint, estimate_context
from wisp.agent.messages import Message
from wisp.providers.base import ToolSpec


@dataclass(frozen=True)
class BenchmarkConfig:
    message_counts: tuple[int, ...] = (100, 1_000, 5_000)
    content_bytes: int = 256
    tool_count: int = 20
    iterations: int = 3
    track_memory: bool = False


@dataclass(frozen=True)
class BenchmarkSample:
    message_count: int
    content_bytes: int
    approximate_input_bytes: int
    estimated_tokens: int
    estimate: Measurement
    fingerprint: Measurement
    estimate_and_fingerprint: Measurement


@dataclass(frozen=True)
class BenchmarkReport:
    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[BenchmarkSample, ...]

    def to_json(self) -> str:
        payload = asdict(self)
        payload["accuracy"] = [asdict(sample) for sample in run_accuracy_benchmark()]
        return json.dumps(payload, indent=2, sort_keys=True)


def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    selected = config or BenchmarkConfig()
    _validate_config(selected)
    tools = _tools(selected.tool_count)
    samples = tuple(_run_sample(count, selected, tools) for count in selected.message_counts)
    return BenchmarkReport(config=selected, environment=environment(), samples=samples)


def _run_sample(
    message_count: int,
    config: BenchmarkConfig,
    tools: tuple[ToolSpec, ...],
) -> BenchmarkSample:
    messages = _messages(message_count, config.content_bytes)
    estimate, estimate_measurement = measure(
        lambda: estimate_context(messages, tools),
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    _fingerprint, fingerprint_measurement = measure(
        lambda: context_fingerprint(messages, tools),
        iterations=config.iterations,
        track_memory=config.track_memory,
    )

    def estimate_and_fingerprint() -> None:
        estimate_context(messages, tools)
        context_fingerprint(messages, tools)

    _result, combined_measurement = measure(
        estimate_and_fingerprint,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    input_bytes = sum(len(message.content.encode("utf-8")) for message in messages)
    input_bytes += sum(
        len(tool.name.encode())
        + len(tool.description.encode())
        + len(json.dumps(tool.input_schema, separators=(",", ":")).encode())
        for tool in tools
    )
    return BenchmarkSample(
        message_count=message_count,
        content_bytes=config.content_bytes,
        approximate_input_bytes=input_bytes,
        estimated_tokens=estimate.total_tokens,
        estimate=estimate_measurement,
        fingerprint=fingerprint_measurement,
        estimate_and_fingerprint=combined_measurement,
    )


def _messages(count: int, content_bytes: int) -> tuple[Message, ...]:
    content = "x" * content_bytes
    return tuple(
        Message(role="user" if index % 2 == 0 else "assistant", content=content)
        for index in range(count)
    )


def _tools(count: int) -> tuple[ToolSpec, ...]:
    return tuple(
        ToolSpec(
            name=f"benchmark_tool_{index}",
            description="A representative benchmark tool schema.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )
        for index in range(count)
    )


def _validate_config(config: BenchmarkConfig) -> None:
    if not config.message_counts or any(count < 1 for count in config.message_counts):
        raise ValueError("message_counts must contain positive counts")
    if config.content_bytes < 0 or config.tool_count < 0 or config.iterations < 1:
        raise ValueError("content_bytes and tool_count must be non-negative; iterations positive")


def _parse_counts(value: str) -> tuple[int, ...]:
    try:
        counts = tuple(int(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("counts must be comma-separated integers") from exc
    if not counts or any(count < 1 for count in counts):
        raise argparse.ArgumentTypeError("counts must be positive integers")
    return counts


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=_parse_counts, default=BenchmarkConfig.message_counts)
    parser.add_argument("--content-bytes", type=int, default=BenchmarkConfig.content_bytes)
    parser.add_argument("--tools", type=int, default=BenchmarkConfig.tool_count)
    parser.add_argument("--iterations", type=int, default=BenchmarkConfig.iterations)
    parser.add_argument("--track-memory", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(
            message_counts=parsed.messages,
            content_bytes=parsed.content_bytes,
            tool_count=parsed.tools,
            iterations=parsed.iterations,
            track_memory=parsed.track_memory,
        )
    )
    payload = report.to_json()
    print(payload)
    if parsed.output is not None:
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])


@dataclass(frozen=True)
class AccuracySample:
    """Fallback-estimator error against a checked-in tokenizer fixture."""

    workload: str
    reference: str
    known_tokens: int
    estimated_tokens: int
    signed_error: int
    absolute_error: int
    error_percent: float
    direction: str


def run_accuracy_benchmark() -> tuple[AccuracySample, ...]:
    """Measure fallback error on representative cl100k_base fixture counts.

    Counts are generated offline from the canonical JSON payloads using tiktoken's
    ``cl100k_base`` encoding. They calibrate this fallback; they are not universal
    guarantees for every provider tokenizer.
    """

    return tuple(_accuracy_sample(*fixture) for fixture in _ACCURACY_FIXTURES)


def _accuracy_sample(
    workload: str,
    messages: tuple[Message, ...],
    tools: tuple[ToolSpec, ...],
    known_tokens: int,
) -> AccuracySample:
    estimated = estimate_context(messages, tools).total_tokens
    error = estimated - known_tokens
    return AccuracySample(
        workload=workload,
        reference="cl100k_base",
        known_tokens=known_tokens,
        estimated_tokens=estimated,
        signed_error=error,
        absolute_error=abs(error),
        error_percent=(error / known_tokens) * 100,
        direction="over" if error > 0 else "under" if error < 0 else "exact",
    )


_ACCURACY_FIXTURES = (
    (
        "source_code",
        (
            Message(
                role="user",
                content=(
                    "Implement:\n```python\ndef greet(name: str) -> str:\n"
                    '    return f"Hello, {name}!"\n```'
                ),
            ),
        ),
        (),
        39,
    ),
    (
        "json_schema",
        (),
        (
            ToolSpec(
                name="create_user",
                description="Create a user record",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "roles": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name"],
                },
            ),
        ),
        47,
    ),
    (
        "large_tool_result",
        (
            Message(role="user", content="Inspect the output."),
            Message(
                role="tool",
                content="line of diagnostic output\n" * 200,
                tool_call_id="call-1",
            ),
        ),
        (),
        1028,
    ),
    (
        "cjk",
        (Message(role="user", content="请分析这个函数并解释为什么它在边界条件下失败。"),),
        (),
        33,
    ),
    (
        "emoji",
        (Message(role="user", content="Debug 👩‍💻🚀 ✅ vs ❌; family 👨‍👩‍👧‍👦 and é"),),
        (),
        47,
    ),
    (
        "mixed_conversation",
        (
            Message(role="user", content="Review 配置 🌍"),
            Message(role="assistant", content="I found two issues."),
            Message(role="user", content='Return JSON: {"修复": true}'),
        ),
        (),
        45,
    ),
)
