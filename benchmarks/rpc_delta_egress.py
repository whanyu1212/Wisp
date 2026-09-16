"""Measure JSONL-RPC delta framing before and after transport coalescing."""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import anyio

from benchmarks.support import Measurement, environment, measure_async
from wisp.cli.rpc_output import RpcEventWriter
from wisp.events import MessageDelta, WispEvent, wisp_event_from_json
from wisp.rpc.framing import encode_rpc_frame
from wisp.rpc.protocol import MAX_LIVE_RPC_FRAME_BYTES


@dataclass(frozen=True)
class BenchmarkConfig:
    response_bytes: int = 256 * 1024
    chunk_bytes: int = 1
    iterations: int = 3


@dataclass(frozen=True)
class EgressSample:
    mode: str
    frames: int
    wire_bytes: int
    content_matches: bool
    egress_and_parse: Measurement


@dataclass(frozen=True)
class BenchmarkReport:
    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[EgressSample, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


async def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    selected = config or BenchmarkConfig()
    if selected.response_bytes < 1 or selected.chunk_bytes < 1 or selected.iterations < 1:
        raise ValueError("response_bytes, chunk_bytes, and iterations must be positive")
    content = "x" * selected.response_bytes
    events = tuple(
        MessageDelta(turn=1, delta=content[start : start + selected.chunk_bytes])
        for start in range(0, len(content), selected.chunk_bytes)
    )
    samples = []
    for mode in ("raw", "coalesced"):
        (frames, wire_bytes, reconstructed), timing = await measure_async(
            lambda selected_mode=mode: _run_egress(events, mode=selected_mode),
            iterations=selected.iterations,
        )
        samples.append(
            EgressSample(
                mode=mode,
                frames=frames,
                wire_bytes=wire_bytes,
                content_matches=reconstructed == content,
                egress_and_parse=timing,
            )
        )
    return BenchmarkReport(
        config=selected,
        environment=environment(),
        samples=tuple(samples),
    )


async def _run_egress(events: tuple[MessageDelta, ...], *, mode: str) -> tuple[int, int, str]:
    output = io.BytesIO()
    frames = 0

    def write_event(event: WispEvent) -> None:
        nonlocal frames
        output.write(encode_rpc_frame(event, max_frame_bytes=MAX_LIVE_RPC_FRAME_BYTES))
        frames += 1

    if mode == "raw":
        for event in events:
            write_event(event)
    else:
        async with anyio.create_task_group() as task_group:
            writer = RpcEventWriter(write_event, task_group)
            for event in events:
                writer(event)
            writer.close()
            task_group.cancel_scope.cancel()

    payload = output.getvalue()
    reconstructed = "".join(
        event.delta
        for line in payload.splitlines()
        if isinstance(event := wisp_event_from_json(line.decode("utf-8")), MessageDelta)
    )
    return frames, len(payload), reconstructed


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response-bytes", type=int, default=BenchmarkConfig.response_bytes)
    parser.add_argument("--chunk-bytes", type=int, default=BenchmarkConfig.chunk_bytes)
    parser.add_argument("--iterations", type=int, default=BenchmarkConfig.iterations)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


async def _main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = await run_benchmark(
        BenchmarkConfig(
            response_bytes=parsed.response_bytes,
            chunk_bytes=parsed.chunk_bytes,
            iterations=parsed.iterations,
        )
    )
    payload = report.to_json()
    print(payload)
    if parsed.output is not None:
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")


def main() -> None:
    anyio.run(_main, sys.argv[1:])


if __name__ == "__main__":
    main()
