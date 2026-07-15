"""Functional tests for periodic (``dispatch_interval``) workflows.

A workflow with ``dispatch_interval`` set is re-run on a recurring schedule,
creating a new ``WorkflowRun`` each time. The pulpcore scheduler polls roughly
once per worker heartbeat (``WORKER_TTL / 3``), so these tests operate on a
timescale of tens of seconds rather than the sub-second ``dispatch_interval``.

Two behaviors are covered:
  * a periodic workflow accumulates multiple runs over time, and stopping it
    halts run creation; and
  * a new run is skipped while a previous run of the same workflow is still in
    progress (no overlapping runs).
"""

import time

# A ``dispatch_interval`` far shorter than the scheduler's poll cadence, so a new
# run is created on essentially every scheduler tick.
FAST_INTERVAL = "00:00:01"

# Generous per-test ceiling; the scheduler ticks about every 10s so a couple of
# runs take ~20-30s to appear.
PERIODIC_TIMEOUT = 180
POLL_SLEEP = 2.0

_FINAL_STATES = {"completed", "failed", "canceled", "skipped"}


def _runs(workflow_bindings, workflow_href):
    """All runs for a workflow, newest first."""
    return workflow_bindings.WorkflowRunsApi.list(workflow_href).results


def _wait_until(predicate, timeout, message):
    """Poll ``predicate`` until it returns a truthy value or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(POLL_SLEEP)
    raise AssertionError(f"Timed out after {timeout}s waiting for: {message}")


def _assert_stable(predicate, window, message):
    """Poll ``predicate`` over ``window`` seconds, failing fast the moment it is false."""
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        assert predicate(), message
        time.sleep(POLL_SLEEP)


def test_periodic_workflow_creates_recurring_runs(workflow_bindings, workflow_factory):
    """A periodic workflow keeps creating runs until it is stopped."""
    # Fast, effectively-no-op tasks so each run completes within one scheduler tick.
    workflow = workflow_factory(
        dispatch_interval=FAST_INTERVAL,
        tasks=[
            {
                "task_name": "pulpcore.app.tasks.orphan_cleanup",
                "task_kwargs": [{"kwarg_key": "orphan_protection_time", "value": 525600}],
            },
        ],
    )

    # Wait until the schedule has produced at least two completed runs.
    def _two_completed():
        completed = [
            r for r in _runs(workflow_bindings, workflow.pulp_href) if r.state == "completed"
        ]
        return len(completed) >= 2

    _wait_until(
        _two_completed,
        PERIODIC_TIMEOUT,
        "at least two completed runs of the periodic workflow",
    )

    # Each recurring run executes as its own run with its own task group.
    runs = _runs(workflow_bindings, workflow.pulp_href)
    run_hrefs = {r.pulp_href for r in runs}
    assert len(run_hrefs) == len(runs), "run hrefs must be unique"
    completed = [r for r in runs if r.state == "completed"]
    assert len(completed) >= 2
    for run in completed:
        assert run.started_at is not None
        assert run.finished_at is not None
        assert run.task_group is not None

    # Stopping the workflow removes its schedule so no further runs are created.
    workflow_bindings.WorkflowsApi.workflows_cancel(workflow.pulp_href, {"state": "canceled"})

    # Let any in-flight run settle into a final state.
    _wait_until(
        lambda: all(r.state in _FINAL_STATES for r in _runs(workflow_bindings, workflow.pulp_href)),
        PERIODIC_TIMEOUT,
        "all runs to reach a final state after stopping",
    )
    count_after_stop = len(_runs(workflow_bindings, workflow.pulp_href))

    # Over the next few scheduler ticks, no new runs should appear; fail fast if one does.
    _assert_stable(
        lambda: len(_runs(workflow_bindings, workflow.pulp_href)) == count_after_stop,
        25,
        "a new run was created after the workflow was stopped",
    )


def test_periodic_workflow_skips_run_while_previous_in_progress(
    workflow_bindings, workflow_factory
):
    """No new run is started while a previous run is still in progress."""
    # A long-running task keeps the first run in-flight across several scheduler
    # ticks, giving the schedule multiple chances to (incorrectly) overlap.
    workflow = workflow_factory(
        dispatch_interval=FAST_INTERVAL,
        tasks=[
            {
                "task_name": "pulpcore.app.tasks.test.sleep",
                "task_kwargs": [{"kwarg_key": "interval", "value": 40}],
            },
        ],
    )

    # Wait for the first run to actually be executing.
    _wait_until(
        lambda: any(r.state == "running" for r in _runs(workflow_bindings, workflow.pulp_href)),
        PERIODIC_TIMEOUT,
        "the first periodic run to reach the running state",
    )

    # Over a window spanning several scheduler ticks, there must never be more
    # than one unfinished run: the schedule skips creating a new run while a
    # previous one is still in progress.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        unfinished = [
            r for r in _runs(workflow_bindings, workflow.pulp_href) if r.state not in _FINAL_STATES
        ]
        assert len(unfinished) <= 1, (
            f"overlapping runs detected: {[(r.pulp_href, r.state) for r in unfinished]}"
        )
        time.sleep(POLL_SLEEP)

    # Stop the workflow so its long-running run is canceled and no more are created.
    workflow_bindings.WorkflowsApi.workflows_cancel(workflow.pulp_href, {"state": "canceled"})


def test_cancel_periodic_workflow_cancels_run_and_stops_schedule(
    workflow_bindings, workflow_factory
):
    """Canceling a periodic workflow cancels its in-flight run and halts the schedule."""
    # A long-running task keeps the run in progress so the cancel lands while it is
    # still executing.
    workflow = workflow_factory(
        dispatch_interval=FAST_INTERVAL,
        tasks=[
            {
                "task_name": "pulpcore.app.tasks.test.sleep",
                "task_kwargs": [{"kwarg_key": "interval", "value": 40}],
            },
        ],
    )

    # Wait for the first run to actually be executing before canceling.
    _wait_until(
        lambda: any(r.state == "running" for r in _runs(workflow_bindings, workflow.pulp_href)),
        PERIODIC_TIMEOUT,
        "the first periodic run to reach the running state",
    )

    # Canceling the workflow removes its schedule and cancels any in-flight run.
    workflow_bindings.WorkflowsApi.workflows_cancel(workflow.pulp_href, {"state": "canceled"})

    # The in-flight run transitions to canceled.
    _wait_until(
        lambda: all(r.state in _FINAL_STATES for r in _runs(workflow_bindings, workflow.pulp_href)),
        PERIODIC_TIMEOUT,
        "all runs to reach a final state after canceling",
    )
    runs_after_cancel = _runs(workflow_bindings, workflow.pulp_href)
    assert any(r.state == "canceled" for r in runs_after_cancel)
    count_after_cancel = len(runs_after_cancel)

    # The schedule is gone: no new runs are created over the next few scheduler ticks.
    _assert_stable(
        lambda: len(_runs(workflow_bindings, workflow.pulp_href)) == count_after_cancel,
        25,
        "a new run was created after the workflow was canceled",
    )
