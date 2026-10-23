"""Offline context/compaction measurement (no provider calls).

Simulates a long tool-heavy session against HistoryManager and reports how
often pruning and summarization fire, how much they save, and what the context
looks like at the end. Use it to compare settings before changing defaults.

    python -m scripts.context_metrics
    python -m scripts.context_metrics --rounds 80 --json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Any

from core.config import Config
from core.context_engine.history_manager import HistoryManager

SUMMARY_TEXT = "## Summary\n" + "decision, file change and open TODO. " * 18


@dataclass
class Scenario:
    name: str
    compression_threshold: float
    tool_prune_enabled: bool
    tool_prune_soft_ratio: float


@dataclass
class Metrics:
    name: str
    rounds: int
    pruned_messages: int
    compactions: int
    summaries: int
    summary_chars: int
    peak_tokens: int
    final_tokens: int


DEFAULT_SCENARIOS = (
    Scenario("current (0.8 / prune 0.5)", 0.8, True, 0.5),
    Scenario("no prune (0.8 / off)", 0.8, False, 0.5),
    Scenario("early summary (0.6 / prune 0.5)", 0.6, True, 0.5),
    Scenario("late summary (0.9 / prune 0.5)", 0.9, True, 0.5),
    Scenario("late prune (0.8 / prune 0.7)", 0.8, True, 0.7),
)


def _round(manager: HistoryManager, index: int, tool_chars: int, text_chars: int = 300) -> None:
    manager.append_user(
        f"Task {index}: adjust the module and report what changed. "
        "Keep existing behaviour intact and add a test. " + "c" * text_chars
    )
    manager.append_assistant(
        "Working on it.",
        metadata={"tool_calls": [{
            "id": f"call_{index}",
            "function": {
                "name": "Bash",
                "arguments": json.dumps({"command": f"python -m pytest -q -k case{index}"}),
            },
        }]},
    )
    manager.append_tool(
        "Bash",
        json.dumps({"status": "success", "data": {"stdout": "x" * tool_chars}}, ensure_ascii=False),
    )
    manager.append_assistant(
        "Done; summary of the change and the next step follows. " + "d" * text_chars,
    )


def run_scenario(
    scenario: Scenario,
    *,
    rounds: int = 60,
    tool_chars: int = 12_000,
    text_chars: int = 300,
    window: int = 128_000,
) -> Metrics:
    config = Config(
        context_window=window,
        compression_threshold=scenario.compression_threshold,
        min_retain_rounds=4,
        tool_prune_enabled=scenario.tool_prune_enabled,
        tool_prune_soft_ratio=scenario.tool_prune_soft_ratio,
        tool_prune_keep_rounds=3,
        tool_prune_min_chars=600,
    )
    manager = HistoryManager(config=config, summary_generator=lambda _m: SUMMARY_TEXT)

    pruned = compactions = summaries = summary_chars = 0
    peak = 0
    for index in range(rounds):
        estimate = manager.estimate_context_tokens("")
        peak = max(peak, estimate)
        if (
            config.tool_prune_enabled
            and estimate >= int(window * config.tool_prune_soft_ratio)
        ):
            pruned += manager.prune_tool_outputs()
        estimate = manager.estimate_context_tokens("")
        if manager.should_compress("", request_token_estimate=estimate):
            info = manager.compact(return_info=True)
            if info.get("compressed"):
                compactions += 1
                if info.get("summary_generated"):
                    summaries += 1
                    summary_chars += int(info.get("summary_len") or 0)
        _round(manager, index, tool_chars, text_chars)

    return Metrics(
        name=scenario.name,
        rounds=rounds,
        pruned_messages=pruned,
        compactions=compactions,
        summaries=summaries,
        summary_chars=summary_chars,
        peak_tokens=peak,
        final_tokens=manager.estimate_context_tokens(""),
    )


def run_all(
    *, rounds: int = 60, tool_chars: int = 12_000, text_chars: int = 300,
    window: int = 128_000,
):
    return [
        run_scenario(
            item, rounds=rounds, tool_chars=tool_chars, text_chars=text_chars, window=window,
        )
        for item in DEFAULT_SCENARIOS
    ]


def render_table(rows: list[Metrics]) -> str:
    header = (
        "| scenario | pruned msgs | compactions | summaries | summary chars | "
        "peak tokens | final tokens |"
    )
    lines = [
        header,
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.name} | {row.pruned_messages} | {row.compactions} | "
            f"{row.summaries} | {row.summary_chars} | {row.peak_tokens} | "
            f"{row.final_tokens} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="context_metrics", description=__doc__)
    parser.add_argument("--rounds", type=int, default=60)
    parser.add_argument("--tool-chars", type=int, default=12_000)
    parser.add_argument("--text-chars", type=int, default=300)
    parser.add_argument("--window", type=int, default=128_000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows = run_all(
        rounds=args.rounds, tool_chars=args.tool_chars,
        text_chars=args.text_chars, window=args.window,
    )
    if args.json:
        print(json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2))
    else:
        print(
            f"rounds={args.rounds} tool_chars={args.tool_chars} "
            f"text_chars={args.text_chars} window={args.window}"
        )
        print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


