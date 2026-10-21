"""A backup drill may only target the explicitly disposable CI database."""
from __future__ import annotations

import pytest

from scripts.postgres_backup_restore_acceptance import _safe_ci_dsn, _with_database


@pytest.fixture
def ci_env(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv(
        "RIDGE_TEST_POSTGRES_DSN",
        "postgresql://ridge_app:fake@127.0.0.1:5432/ridge_ci",
    )


def test_drill_requires_explicit_ci_and_exact_local_database(ci_env, monkeypatch):
    assert _safe_ci_dsn("RIDGE_TEST_POSTGRES_DSN") == (
        "postgresql://ridge_app:fake@127.0.0.1:5432/ridge_ci",
        "ridge_app", 5432, "fake", "127.0.0.1",
    )
    for value in (
        "postgresql://ridge_app:fake@prod.example.com:5432/ridge_ci",
        "postgresql://ridge_app:fake@127.0.0.1:5432/prod",
        "postgresql://ridge_app:fake@127.0.0.1:5433/ridge_ci",
        "postgresql://ridge_app@127.0.0.1:5432/ridge_ci",
        "postgresql://ridge_app:fake@127.0.0.1:5432/ridge_ci?sslmode=prefer",
        "sqlite:///ridge_ci",
    ):
        monkeypatch.setenv("RIDGE_TEST_POSTGRES_DSN", value)
        with pytest.raises(RuntimeError, match="disposable"):
            _safe_ci_dsn("RIDGE_TEST_POSTGRES_DSN")
    monkeypatch.setenv(
        "RIDGE_TEST_POSTGRES_DSN",
        "postgresql://ridge_app:fake@127.0.0.1:5432/ridge_ci",
    )
    monkeypatch.delenv("CI")
    with pytest.raises(RuntimeError, match="disposable"):
        _safe_ci_dsn("RIDGE_TEST_POSTGRES_DSN")


def test_restore_name_is_server_generated_not_user_controlled(ci_env):
    restored = _with_database(
        "postgresql://ridge_app:fake@127.0.0.1:5432/ridge_ci",
        "ridge_dr_abcdef123456",
    )
    assert restored.endswith("/ridge_dr_abcdef123456")
