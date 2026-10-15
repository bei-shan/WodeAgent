"""Lifecycle transaction: cleanup on partial init, post-init failures and repeat close."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.features.base import AgentFeature
from core.features.lifecycle import FeatureLifecycle


def feature(name, events, *, fail_phase=None, fail_cleanup=False):
    class F(AgentFeature):
        def init(self, agent):
            events.append((name, "init"))
            if fail_phase == "init":
                raise RuntimeError("init exploded")

        def post_init(self, agent):
            events.append((name, "post"))
            if fail_phase == "post":
                raise RuntimeError("post exploded")

        def cleanup(self, agent):
            events.append((name, "cleanup"))
            if fail_cleanup:
                raise RuntimeError("cleanup exploded")

    result = F()
    result.name = name
    return result


def test_feature_lifecycle_orders_phases_and_closes_once():
    events = []
    run = FeatureLifecycle(
        SimpleNamespace(),
        [feature("a", events), feature("b", events)],
    )
    run.initialize()
    run.post_initialize()
    run.close()
    run.close()
    assert events == [
        ("a", "init"), ("b", "init"),
        ("a", "post"), ("b", "post"),
        ("b", "cleanup"), ("a", "cleanup"),
    ]
    assert len(run.active) == 2
    with pytest.raises(RuntimeError):
        run.initialize()


def test_failed_init_cleans_partial_feature_but_never_unentered():
    events = []
    run = FeatureLifecycle(
        SimpleNamespace(),
        [feature("a", events), feature("b", events, fail_phase="init"),
         feature("never", events)],
    )
    with pytest.raises(RuntimeError, match="init exploded"):
        run.initialize()
    run.close()
    assert events == [
        ("a", "init"), ("b", "init"),
        ("b", "cleanup"), ("a", "cleanup"),
    ]


def test_failed_post_init_cleans_all_initialized_features():
    events = []
    run = FeatureLifecycle(
        SimpleNamespace(),
        [feature("a", events), feature("b", events, fail_phase="post")],
    )
    run.initialize()
    with pytest.raises(RuntimeError, match="post exploded"):
        run.post_initialize()
    run.close()
    assert events[-2:] == [("b", "cleanup"), ("a", "cleanup")]


def test_cleanup_errors_do_not_skip_remaining_resources(caplog):
    events = []
    run = FeatureLifecycle(
        SimpleNamespace(),
        [feature("a", events), feature("b", events, fail_cleanup=True)],
    )
    run.initialize()
    run.close()
    assert events[-2:] == [("b", "cleanup"), ("a", "cleanup")]
    assert "Feature b cleanup failed" in caplog.text


def test_lifecycle_rejects_non_features_and_post_init_before_init():
    with pytest.raises(TypeError):
        FeatureLifecycle(object(), [object()])
    run = FeatureLifecycle(object(), [])
    with pytest.raises(RuntimeError):
        run.post_initialize()
