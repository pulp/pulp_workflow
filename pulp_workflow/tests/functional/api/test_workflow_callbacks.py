"""End-to-end test that runs a Workflow with CallbackServices.

The workflow syncs a file repository. It has callbacks attached for the ``completed`` and
``finished`` lifecycle events; both should fire and their dispatched tasks should complete
successfully. Workflow context (the workflow's name, state, and labels) is exposed to the callback
script via environment variables; the test sets a ``email=user@example.com`` label to exercise
the ``PULP_WORKFLOW_LABEL_EMAIL`` path.
"""

import time
import uuid

import pytest

from pulpcore.plugin.util import extract_pk

POLL_INTERVAL_SECONDS = 2.0


def test_workflow_with_callback_on_sync(
    workflow_bindings,
    pulpcore_bindings,
    file_bindings,
    file_repo,
    file_remote_factory,
    basic_manifest_path,
    workflow_factory,
    callback_service_factory,
    monitor_task,
    monitor_workflow,
):
    """A workflow that syncs a file repo and notifies a callback on completion.

    Mirrors the user-story in the issue: an admin registers a CallbackService pointing at a script
    (here ``/bin/echo``) and attaches it to a workflow that syncs a file repo. The workflow's
    email recipient lives in a ``email`` label and is exposed to the callback as
    ``PULP_WORKFLOW_LABEL_EMAIL``.
    """
    repo = file_repo
    remote = file_remote_factory(manifest_path=basic_manifest_path, policy="immediate")

    callback_service = callback_service_factory(script="/bin/echo")

    workflow = workflow_factory(
        pulp_labels={"email": "user@example.com"},
        tasks=[
            {
                "task_name": "pulp_file.app.tasks.synchronizing.synchronize",
                "task_kwargs": [
                    {
                        "kwarg_key": "repository_pk",
                        "value": extract_pk(repo.pulp_href),
                    },
                    {
                        "kwarg_key": "remote_pk",
                        "value": extract_pk(remote.pulp_href),
                    },
                    {"kwarg_key": "mirror", "value": False},
                ],
                "reserved_resources": [repo.pulp_href],
            },
        ],
        callbacks=[
            {
                "callback_service": callback_service.pulp_href,
                "callback_type": "completed",
            },
            {
                "callback_service": callback_service.pulp_href,
                "callback_type": "finished",
            },
        ],
    )

    finished = monitor_workflow(workflow.pulp_href)

    # ---- Workflow-level assertions.
    assert finished.pulp_labels == {"email": "user@example.com"}
    assert len(finished.tasks) == 1

    # ---- The sync task itself ran.
    sync_task = pulpcore_bindings.TasksApi.read(finished.tasks[0].dispatched_task)
    assert sync_task.state == "completed"
    assert sync_task.name == "pulp_file.app.tasks.synchronizing.synchronize"

    # ---- Both callbacks fired and completed.
    assert len(finished.callbacks) == 2
    callback_types = sorted(cb.callback_type for cb in finished.callbacks)
    assert callback_types == ["completed", "finished"]
    for cb in finished.callbacks:
        assert cb.callback_service == callback_service.pulp_href, (
            f"Unexpected callback_service: {cb.callback_service!r}"
        )
        assert cb.dispatched_task is not None, f"Callback {cb.callback_type} was not dispatched"
        # monitor_task raises PulpTaskError if the task ends in any non-completed state.
        cb_task = monitor_task(cb.dispatched_task)
        assert cb_task.name == "pulp_workflow.app.tasks.run_callback"


def test_workflow_callback_fires_on_cancel(
    workflow_bindings,
    pulpcore_bindings,
    workflow_factory,
    callback_service_factory,
    monitor_task,
):
    """A canceled-before-start workflow fires its CANCELED + FINISHED callbacks."""
    from datetime import datetime, timedelta, timezone

    callback_service = callback_service_factory(script="/bin/echo")

    # Schedule far enough in the future that we can cancel before it starts.
    start_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    workflow = workflow_factory(
        start_time=start_time,
        callbacks=[
            {
                "callback_service": callback_service.pulp_href,
                "callback_type": "canceled",
            },
            {
                "callback_service": callback_service.pulp_href,
                "callback_type": "finished",
            },
            # 'completed' should NOT fire when the workflow is canceled.
            {
                "callback_service": callback_service.pulp_href,
                "callback_type": "completed",
            },
        ],
    )

    canceled = workflow_bindings.WorkflowsApi.workflows_cancel(
        workflow.pulp_href, {"state": "canceled"}
    )
    assert canceled.state == "canceled"

    # Re-fetch so we see the dispatched_task hrefs the cancel set asynchronously.
    fired_types = set()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        latest = workflow_bindings.WorkflowsApi.read(workflow.pulp_href)
        fired_types = {cb.callback_type for cb in latest.callbacks if cb.dispatched_task}
        if {"canceled", "finished"}.issubset(fired_types):
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    assert {"canceled", "finished"}.issubset(fired_types), (
        f"Expected canceled+finished callbacks to fire, only fired: {fired_types}"
    )
    # The 'completed' callback must not have fired.
    completed_cb = next(cb for cb in latest.callbacks if cb.callback_type == "completed")
    assert completed_cb.dispatched_task is None

    # The fired callbacks should run to completion.
    for cb in latest.callbacks:
        if not cb.dispatched_task:
            continue
        monitor_task(cb.dispatched_task)


def test_workflow_callback_duplicate_type_rejected(workflow_bindings, callback_service_factory):
    """Two callbacks for the same (callback_service, callback_type) on one workflow are rejected."""
    callback_service = callback_service_factory()
    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.WorkflowsApi.create(
            {
                "name": str(uuid.uuid4()),
                "tasks": [{"task_name": "pulpcore.app.tasks.orphan_cleanup"}],
                "callbacks": [
                    {
                        "callback_service": callback_service.pulp_href,
                        "callback_type": "completed",
                    },
                    {
                        "callback_service": callback_service.pulp_href,
                        "callback_type": "completed",
                    },
                ],
            }
        )
    assert exc.value.status == 400


def test_workflow_callback_invalid_type_rejected(workflow_bindings, callback_service_factory):
    """A callback_type outside of the documented choices is rejected.

    The bindings declare ``callback_type`` as a closed enum, so an invalid value is caught
    client-side by pydantic before the request is sent. We accept either that pydantic
    ``ValidationError`` or a server-side 400 (when called via raw HTTP).
    """
    from pydantic import ValidationError

    callback_service = callback_service_factory()
    with pytest.raises((ValidationError, workflow_bindings.ApiException)):
        workflow_bindings.WorkflowsApi.create(
            {
                "name": str(uuid.uuid4()),
                "tasks": [{"task_name": "pulpcore.app.tasks.orphan_cleanup"}],
                "callbacks": [
                    {
                        "callback_service": callback_service.pulp_href,
                        "callback_type": "not-a-real-state",
                    },
                ],
            }
        )
