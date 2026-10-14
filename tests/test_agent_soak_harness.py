"""Offline soak benchmark contract and deterministic thresholds."""
from __future__ import annotations

import json

import pytest

from scripts.agent_soak import main, run_soak, snapshot


def test_short_repeatable_soak_runs_real_agent_session_workers():
    report = run_soak(iterations=4, duration_seconds=0, max_rss_growth_mib=200)
    assert report["result"] == "pass", report
    assert report["cycles"] == 4
    assert report["mode"] == "offline_fake_provider"
    assert report["errors"] == []
    assert report["growth"]["threads"] <= 5


def test_cli_soak_report_machine_readable_and_contains_no_user_content(capsys):
    assert main(["--iterations", "3", "--duration-seconds", "0",
                 "--max-rss-growth-mib", "200", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["cycles"] == 3
    assert result["start"]["threads"] >= 1
    assert result["result"] == "pass"
    assert "soak turn" not in json.dumps(result)


def test_invalid_budget_and_window_are_rejected():
    with pytest.raises(ValueError):
        run_soak(iterations=0, duration_seconds=0)
    with pytest.raises(ValueError):
        run_soak(iterations=2, max_fd_growth=-1)
    with pytest.raises(ValueError):
        run_soak(iterations=-1)


def test_snapshot_is_bounded_numeric_metrics_only():
    data = snapshot()
    assert set(data) == {"threads", "fds", "rss_bytes"}
    assert data["threads"] > 0
    assert all(v is None or isinstance(v, int) for v in data.values())
