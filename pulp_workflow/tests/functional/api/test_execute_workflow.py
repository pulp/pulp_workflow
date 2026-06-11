"""End-to-end test that runs a Workflow against pulp_file.

The workflow has two tasks:
    0. Add ``content_a`` to a file repository (creates repository version 1).
    1. Publish that repository version. The ``repository_version_pk`` kwarg is
       a dynamic arg (``content_type`` set) that resolves at dispatch time to
       the unique ``RepositoryVersion`` created by task 0.
"""

import json
import time
import uuid

import pytest

from pulpcore.plugin.util import extract_pk

from pulp_workflow.pytest_plugin import (
    WORKFLOW_FINAL_STATES,
    WORKFLOW_SLEEP_TIME,
    WORKFLOW_TIMEOUT,
)


def _wait_for_workflow(api, workflow_href, timeout=WORKFLOW_TIMEOUT):
    """Poll a Workflow until it reaches a final state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        workflow = api.read(workflow_href)
        if workflow.state in WORKFLOW_FINAL_STATES:
            return workflow
        time.sleep(WORKFLOW_SLEEP_TIME)
    raise AssertionError(f"Workflow {workflow_href} did not finish within {timeout}s")


def test_execute_workflow_add_content_and_publish(
    workflow_bindings,
    pulpcore_bindings,
    file_bindings,
    file_repo,
    file_content_unit_with_name_factory,
    workflow_factory,
    monitor_workflow,
):
    """A Workflow that adds content then publishes the new version end-to-end."""
    repo = file_repo
    content_a = file_content_unit_with_name_factory(str(uuid.uuid4()))

    workflow = workflow_factory(
        tasks=[
            {
                "task_name": "pulpcore.app.tasks.repository.add_and_remove",
                "task_kwargs": [
                    {
                        "kwarg_key": "repository_pk",
                        "value": extract_pk(repo.pulp_href),
                    },
                    {
                        "kwarg_key": "add_content_units",
                        "value": [extract_pk(content_a.pulp_href)],
                    },
                    {"kwarg_key": "remove_content_units", "value": []},
                ],
                "reserved_resources": [repo.pulp_href],
            },
            {
                "task_name": "pulp_file.app.tasks.publish",
                "task_kwargs": [
                    {"kwarg_key": "manifest", "value": "PULP_MANIFEST"},
                    # Resolved at dispatch time to the pk of the RepositoryVersion
                    # created by task 0.
                    {
                        "kwarg_key": "repository_version_pk",
                        "content_type": "core.repositoryversion",
                    },
                ],
                "reserved_resources": [repo.pulp_href],
            },
        ],
    )

    finished = monitor_workflow(workflow.pulp_href)

    # ---- Workflow-level assertions.
    assert finished.error is None
    assert finished.started_at is not None
    assert finished.finished_at is not None
    assert finished.finished_at >= finished.started_at
    assert finished.current_task is None
    assert len(finished.tasks) == 2

    # ---- TaskGroup membership and dispatched state.
    assert finished.task_group is not None
    task_group = pulpcore_bindings.TaskGroupsApi.read(finished.task_group)
    assert task_group.all_tasks_dispatched is True
    group_task_hrefs = {t.pulp_href for t in task_group.tasks}
    # Every child task is in the group.
    assert finished.tasks[0].dispatched_task in group_task_hrefs
    assert finished.tasks[1].dispatched_task in group_task_hrefs
    # The execute_workflow continuations are also in the group: 1 per step + 1 final.
    # (2 child tasks + at least 2 execute_workflow continuations.)
    assert len(group_task_hrefs) >= 4

    task0, task1 = finished.tasks[0], finished.tasks[1]
    assert task0.dispatched_task is not None
    assert task1.dispatched_task is not None

    # ---- Each task's child task ran with the right resource.
    task0_task = pulpcore_bindings.TasksApi.read(task0.dispatched_task)
    task1_task = pulpcore_bindings.TasksApi.read(task1.dispatched_task)
    assert task0_task.state == "completed"
    assert task0_task.name == "pulpcore.app.tasks.repository.add_and_remove"
    assert repo.pulp_href in (task0_task.reserved_resources_record or [])
    assert task1_task.state == "completed"
    assert task1_task.name == "pulp_file.app.tasks.publish"
    assert repo.pulp_href in (task1_task.reserved_resources_record or [])

    # Each step is dispatched by its own execute_workflow continuation, so each
    # child has a parent_task but they are not necessarily the same one.
    assert task0_task.parent_task is not None
    assert task1_task.parent_task is not None

    # Task 0 produced version 1.
    task0_versions = [h for h in (task0_task.created_resources or []) if "/versions/" in h]
    assert len(task0_versions) == 1
    version_href = task0_versions[0]
    assert version_href.endswith("/versions/1/")

    version = file_bindings.RepositoriesFileVersionsApi.read(version_href)
    assert version.content_summary.added.get("file.file", {}).get("count") == 1

    # ---- Repo's latest version is the new one.
    refreshed_repo = file_bindings.RepositoriesFileApi.read(repo.pulp_href)
    assert refreshed_repo.latest_version_href == version_href

    # ---- Task 1 produced a publication for that version.
    task1_publications = [h for h in (task1_task.created_resources or []) if "/publications/" in h]
    assert len(task1_publications) == 1
    publication = file_bindings.PublicationsFileApi.read(task1_publications[0])
    assert publication.repository_version == version_href
    assert publication.manifest == "PULP_MANIFEST"


def _wait_for_state(api, workflow_href, predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        workflow = api.read(workflow_href)
        if predicate(workflow):
            return workflow
        time.sleep(0.5)
    raise AssertionError(f"Workflow {workflow_href} did not satisfy predicate within {timeout}s")


def _many_orphan_cleanup_tasks(n):
    safe_kwargs = [{"kwarg_key": "orphan_protection_time", "value": 525600}]
    return [
        {"task_name": "pulpcore.app.tasks.orphan_cleanup", "task_kwargs": safe_kwargs}
        for _ in range(n)
    ]


def _many_sleep_tasks(n, seconds=15):
    # Long-running tasks so cancellation has a deterministic window to land
    # while tasks are still in-flight or queued.
    return [
        {
            "task_name": "pulpcore.app.tasks.test.sleep",
            "task_kwargs": [{"kwarg_key": "interval", "value": seconds}],
        }
        for _ in range(n)
    ]


def test_cancel_running_workflow_via_patch(workflow_bindings, pulpcore_bindings, workflow_factory):
    """A RUNNING workflow can be canceled via PATCH; in-flight tasks are canceled."""
    workflow = workflow_factory(tasks=_many_sleep_tasks(15))

    running = _wait_for_state(
        workflow_bindings.WorkflowsApi,
        workflow.pulp_href,
        lambda w: w.state in {"running", "completed", "failed", "canceled"},
        timeout=60,
    )
    # If the workflow happened to finish before we could observe RUNNING, the test
    # is meaningless; require it to actually have entered RUNNING.
    assert running.state == "running", f"workflow finished too quickly: state={running.state!r}"

    canceled = workflow_bindings.WorkflowsApi.workflows_cancel(
        workflow.pulp_href, {"state": "canceled"}
    )
    assert canceled.state == "canceled"
    assert canceled.finished_at is not None

    finished = _wait_for_workflow(workflow_bindings.WorkflowsApi, workflow.pulp_href)
    assert finished.state == "canceled"
    assert finished.task_group is not None

    task_group = pulpcore_bindings.TaskGroupsApi.read(finished.task_group)
    assert task_group.all_tasks_dispatched is True
    # Some tasks in the group must have ended in CANCELED (or CANCELING) — cancel_task_group
    # cancels every in-flight or queued task in the group.
    states = {t.state for t in task_group.tasks}
    assert "canceled" in states or "canceling" in states


def test_cancel_running_workflow_via_task_group(
    workflow_bindings, pulpcore_bindings, workflow_factory
):
    """Canceling the underlying TaskGroup propagates CANCELED to the Workflow."""
    workflow = workflow_factory(tasks=_many_sleep_tasks(15))

    running = _wait_for_state(
        workflow_bindings.WorkflowsApi,
        workflow.pulp_href,
        lambda w: w.state in {"running", "completed", "failed", "canceled"},
        timeout=60,
    )
    assert running.state == "running", f"workflow finished too quickly: state={running.state!r}"
    assert running.task_group is not None

    pulpcore_bindings.TaskGroupsApi.task_groups_cancel(running.task_group, {"state": "canceled"})

    finished = _wait_for_workflow(workflow_bindings.WorkflowsApi, workflow.pulp_href)
    assert finished.state == "canceled"
    assert finished.finished_at is not None


def test_cancel_terminal_workflow_returns_409(workflow_bindings, workflow_factory):
    """Canceling an already-terminal workflow returns 409 Conflict."""
    workflow = workflow_factory(tasks=_many_orphan_cleanup_tasks(2))
    finished = _wait_for_workflow(workflow_bindings.WorkflowsApi, workflow.pulp_href)
    assert finished.state == "completed"

    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.WorkflowsApi.workflows_cancel(workflow.pulp_href, {"state": "canceled"})
    assert exc.value.status == 409


# Keys ``_fail_workflow`` is permitted to write into ``workflow.error``.
_ALLOWED_ERROR_KEYS = {
    "task_index",
    "task_name",
    "description",
    "traceback",
    "child_error",
}


def test_failed_workflow_records_child_error_for_bad_task_args(
    workflow_bindings, pulpcore_bindings, workflow_factory
):
    """A child task that fails because of bad args surfaces a well-formed error payload."""
    bogus_repo_pk = str(uuid.uuid4())
    workflow = workflow_factory(
        tasks=[
            {
                "task_name": "pulpcore.app.tasks.repository.add_and_remove",
                "task_kwargs": [
                    {"kwarg_key": "repository_pk", "value": bogus_repo_pk},
                    {"kwarg_key": "add_content_units", "value": []},
                    {"kwarg_key": "remove_content_units", "value": []},
                ],
            },
        ],
    )

    finished = _wait_for_workflow(workflow_bindings.WorkflowsApi, workflow.pulp_href)
    assert finished.state == "failed"
    assert finished.finished_at is not None
    assert finished.current_task is not None

    error = finished.error
    assert isinstance(error, dict)
    assert set(error).issubset(_ALLOWED_ERROR_KEYS)
    assert error["task_index"] == 0
    assert error["task_name"] == "pulpcore.app.tasks.repository.add_and_remove"
    assert isinstance(error["description"], str) and error["description"]
    # No "traceback" key for the child-failure path: it is only set when
    # "_fail_workflow" is called with "exc=" (the dispatch-time path).
    assert "traceback" not in error

    child_task_href = finished.tasks[0].dispatched_task
    assert child_task_href is not None
    child_task = pulpcore_bindings.TasksApi.read(child_task_href)
    assert child_task.state == "failed"
    assert error["child_error"] == child_task.error


def test_failed_workflow_error_does_not_leak_task_arg_values(workflow_bindings, workflow_factory):
    """A sentinel value passed in task kwargs must not appear in workflow.error"""
    sentinel = f"audit-sentinel-{uuid.uuid4()}"
    workflow = workflow_factory(
        tasks=[
            {
                "task_name": "pulpcore.app.tasks.repository.add_and_remove",
                "task_kwargs": [
                    {"kwarg_key": "repository_pk", "value": str(uuid.uuid4())},
                    {"kwarg_key": "add_content_units", "value": [sentinel]},
                    {"kwarg_key": "remove_content_units", "value": []},
                ],
            },
        ],
    )

    finished = _wait_for_workflow(workflow_bindings.WorkflowsApi, workflow.pulp_href)
    assert finished.state == "failed"
    serialized = json.dumps(finished.error, default=str)
    assert sentinel not in serialized, f"workflow.error leaked task arg value: {serialized!r}"


def test_failed_workflow_dispatch_traceback_does_not_leak_task_arg_values(
    workflow_bindings, workflow_factory
):
    """Dispatch-time failure (unresolvable dynamic kwarg) sets ``traceback`` without leaking args."""
    sentinel = f"audit-sentinel-{uuid.uuid4()}"
    workflow = workflow_factory(
        tasks=[
            {
                "task_name": "pulpcore.app.tasks.orphan_cleanup",
                "task_kwargs": [{"kwarg_key": "orphan_protection_time", "value": 525600}],
            },
            {
                "task_name": "pulpcore.app.tasks.orphan_cleanup",
                "task_kwargs": [
                    {
                        "kwarg_key": sentinel,
                        "content_type": "core.repositoryversion",
                    },
                ],
            },
        ],
    )

    finished = _wait_for_workflow(workflow_bindings.WorkflowsApi, workflow.pulp_href)
    assert finished.state == "failed"

    error = finished.error
    assert isinstance(error, dict)
    assert set(error).issubset(_ALLOWED_ERROR_KEYS)
    assert error["task_index"] == 1
    assert "traceback" in error and isinstance(error["traceback"], str)
    # Dispatch-time path does not have a child task to pull error info from.
    assert "child_error" not in error

    serialized = json.dumps(error, default=str)
    assert sentinel not in serialized, (
        f"workflow.error leaked dynamic kwarg key into traceback: {serialized!r}"
    )
