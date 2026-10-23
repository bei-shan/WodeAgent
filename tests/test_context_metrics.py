"""Offline compaction measurement script."""
from __future__ import annotations

import json

from scripts.context_metrics import (
    Scenario,
    main,
    render_table,
    run_all,
    run_scenario,
)


def test_tool_heavy_session_compacts_without_pruning():
    scenario = Scenario("no prune", compression_threshold=0.8, tool_prune_enabled=False,
                        tool_prune_soft_ratio=0.5)
    metrics = run_scenario(scenario, rounds=24, tool_chars=12_000, text_chars=200, window=32_000)
    assert metrics.compactions >= 1
    assert metrics.summaries >= 1
    assert metrics.pruned_messages == 0


def test_pruning_keeps_tool_heavy_sessions_under_the_summary_threshold():
    scenario = Scenario("prune", compression_threshold=0.8, tool_prune_enabled=True,
                        tool_prune_soft_ratio=0.5)
    metrics = run_scenario(scenario, rounds=24, tool_chars=12_000, text_chars=200, window=128_000)
    assert metrics.pruned_messages > 0
    assert metrics.compactions == 0
    assert metrics.peak_tokens < int(128_000 * 0.8)


def test_render_table_lists_every_scenario():
    rows = [run_scenario(item, rounds=8, tool_chars=4000, window=64_000)
            for item in (
                Scenario("a", 0.8, True, 0.5),
                Scenario("b", 0.8, False, 0.5),
            )]
    table = render_table(rows)
    assert table.splitlines()[0].startswith("| scenario")
    assert "| a |" in table and "| b |" in table


def test_cli_json_output(capsys):
    assert main(["--rounds", "6", "--tool-chars", "4000", "--window", "64000", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == len(run_all.__defaults__ or ()) or len(payload) >= 3
    assert {"name", "compactions", "pruned_messages", "final_tokens"} <= set(payload[0])
