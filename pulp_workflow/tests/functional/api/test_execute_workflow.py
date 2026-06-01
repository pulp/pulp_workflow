"""End-to-end test that runs a Workflow against pulp_file.

The workflow has two tasks:
    0. Add ``content_a`` to a file repository (creates repository version 1).
    1. Publish that repository version. The ``repository_version_pk`` kwarg is
       a dynamic arg (``content_type`` set) that resolves at dispatch time to
       the unique ``RepositoryVersion`` created by task 0.
"""

import time
import uuid

WORKFLOW_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 2.0


def _pk_from_href(href):
    return href.rstrip("/").split("/")[-1]


def _wait_for_workflow(api, workflow_href, timeout=WORKFLOW_TIMEOUT_SECONDS):
    """Poll a Workflow until it reaches a final state."""
    final_states = {"completed", "failed", "canceled", "skipped"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        workflow = api.read(workflow_href)
        if workflow.state in final_states:
            return workflow
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"Workflow {workflow_href} did not finish within {timeout}s")


def test_execute_workflow_add_content_and_publish(
    workflow_bindings,
    pulpcore_bindings,
    file_bindings,
    file_repo,
    file_content_unit_with_name_factory,
    workflow_factory,
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
                        "value": _pk_from_href(repo.pulp_href),
                    },
                    {
                        "kwarg_key": "add_content_units",
                        "value": [_pk_from_href(content_a.pulp_href)],
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

    finished = _wait_for_workflow(workflow_bindings.WorkflowsApi, workflow.pulp_href)

    # ---- Workflow-level assertions.
    assert finished.state == "completed", (
        f"Workflow state={finished.state!r} error={finished.error!r}"
    )
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
