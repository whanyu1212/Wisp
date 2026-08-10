"""Measure cold and warm JSONL session loading paths."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmarks.support import Measurement, environment, measure, measure_async
from wisp.agent.messages import Message
from wisp.sessions.jsonl import JsonlSessionStore


@dataclass(frozen=True)
class BenchmarkConfig:
    entry_counts: tuple[int, ...] = (2_000, 10_000)
    page_size: int = 300
    iterations: int = 3
    track_memory: bool = False


@dataclass(frozen=True)
class BenchmarkSample:
    entry_count: int
    session_bytes: int
    cold_newest_page: Measurement
    warm_newest_page: Measurement
    complete_parse: Measurement
    context_replay: Measurement
    older_page: Measurement | None
    indexed_append: Measurement


@dataclass(frozen=True)
class BenchmarkReport:
    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[BenchmarkSample, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


async def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    selected = config or BenchmarkConfig()
    _validate_config(selected)
    with tempfile.TemporaryDirectory(prefix="wisp-session-benchmark-") as temporary_directory:
        root = Path(temporary_directory)
        samples = []
        for entry_count in selected.entry_counts:
            samples.append(await _run_sample(root / str(entry_count), entry_count, selected))
    return BenchmarkReport(config=selected, environment=environment(), samples=tuple(samples))


async def _run_sample(
    root: Path,
    entry_count: int,
    config: BenchmarkConfig,
) -> BenchmarkSample:
    store = JsonlSessionStore(root)
    session = store.create()
    for index in range(entry_count):
        await session.append_message(
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=f"benchmark message {index}",
            )
        )

    cold = store.load(session.path)
    newest_page, cold_measurement = measure(
        lambda: cold.read_message_page(limit=config.page_size),
        track_memory=config.track_memory,
    )
    _warm_page, warm_measurement = measure(
        lambda: cold.read_message_page(limit=config.page_size),
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    parse_session = store.load(session.path)
    entries, parse_measurement = measure(
        parse_session.read_entries,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    replay_session = store.load(session.path)
    replay, replay_measurement = measure(
        replay_session.read_context,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    if len(entries) != entry_count or len(replay.messages) != entry_count:
        raise RuntimeError("session benchmark fixture lost entries during parse or replay")
    session_bytes = session.path.stat().st_size

    older_measurement: Measurement | None = None
    if newest_page.next_before_entry_id is not None:
        _older_page, older_measurement = measure(
            lambda: cold.read_message_page(
                limit=config.page_size,
                before_entry_id=newest_page.next_before_entry_id,
            ),
            iterations=config.iterations,
            track_memory=config.track_memory,
        )

    async def append_message() -> object:
        return await cold.append_message(Message(role="user", content="indexed append probe"))

    _entry, append_measurement = await measure_async(
        append_message,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    return BenchmarkSample(
        entry_count=entry_count,
        session_bytes=session_bytes,
        cold_newest_page=cold_measurement,
        warm_newest_page=warm_measurement,
        complete_parse=parse_measurement,
        context_replay=replay_measurement,
        older_page=older_measurement,
        indexed_append=append_measurement,
    )


def _validate_config(config: BenchmarkConfig) -> None:
    if not config.entry_counts or any(count < 1 for count in config.entry_counts):
        raise ValueError("entry_counts must contain positive counts")
    if config.page_size < 1 or config.iterations < 1:
        raise ValueError("page_size and iterations must be positive")


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
    parser.add_argument("--entries", type=_parse_counts, default=BenchmarkConfig.entry_counts)
    parser.add_argument("--page-size", type=int, default=BenchmarkConfig.page_size)
    parser.add_argument("--iterations", type=int, default=BenchmarkConfig.iterations)
    parser.add_argument("--track-memory", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


async def _main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = await run_benchmark(
        BenchmarkConfig(
            entry_counts=parsed.entries,
            page_size=parsed.page_size,
            iterations=parsed.iterations,
            track_memory=parsed.track_memory,
        )
    )
    payload = report.to_json()
    print(payload)
    if parsed.output is not None:
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")


def main() -> None:
    import anyio

    anyio.run(_main, sys.argv[1:])


if __name__ == "__main__":
    main()
