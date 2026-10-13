"""Deterministic safety and ordering checks for tool batch scheduling."""
import threading

from core.runtime.tool_scheduler import plan_tool_batches, run_tool_batch


def test_safe_runs_do_not_cross_unsafe_barriers():
    calls = ["ReadA", "ReadB", "Write", "ReadC", "ReadD", "Bash", "ReadE"]
    batches = plan_tool_batches(calls, lambda name: name.startswith("Read"))
    assert [(b.calls, b.concurrent) for b in batches] == [
        (("ReadA", "ReadB"), True),
        (("Write",), False),
        (("ReadC", "ReadD"), True),
        (("Bash",), False),
        (("ReadE",), False),
    ]


def test_unknown_or_broken_safety_classification_is_serial():
    def policy(name):
        if name == "bad":
            raise RuntimeError("unknown")
        return name == "safe"

    batches = plan_tool_batches(["safe", "bad", "safe"], policy)
    assert len(batches) == 3
    assert all(not b.concurrent for b in batches)
    assert all(not b.concurrent for b in plan_tool_batches(["safe", "safe"], policy, max_workers=1))


def test_concurrent_execution_overlaps_but_results_keep_model_order():
    barrier = threading.Barrier(2, timeout=2)
    batch = plan_tool_batches(["first", "second"], lambda _: True)[0]
    main_thread = threading.get_ident()
    workers = []

    def worker(name):
        workers.append(threading.get_ident())
        barrier.wait()
        return name.upper()

    assert run_tool_batch(batch, worker) == ["FIRST", "SECOND"]
    assert len(workers) == 2
    assert all(w != main_thread for w in workers)


def test_serial_tools_never_leave_the_caller_thread():
    batches = plan_tool_batches(["Write", "Bash"], lambda _: False)
    main_thread = threading.get_ident()
    assert [run_tool_batch(b, lambda _: threading.get_ident()) for b in batches] == [
        [main_thread], [main_thread],
    ]
