"""End-to-end test that runs a TaskPlan against pulp_file.

The plan has two steps:
    0. Add ``content_a`` to a file repository (creates repository version 1).
    1. Publish that repository version. The ``repository_version_pk`` is supplied
       at runtime via a ``$prev_resource`` marker that resolves to the unique
       ``RepositoryVersion`` created by step 0.
"""

import time
import uuid

import pytest

PLAN_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 2.0


def _pk_from_href(href):
    return href.rstrip("/").split("/")[-1]


def _wait_for_plan(api, plan_href, timeout=PLAN_TIMEOUT_SECONDS):
    """Poll a TaskPlan until it reaches a final state."""
    final_states = {"completed", "failed", "canceled", "skipped"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        plan = api.read(plan_href)
        if plan.state in final_states:
            return plan
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"TaskPlan {plan_href} did not finish within {timeout}s")


@pytest.mark.parallel
def test_execute_task_plan_add_content_and_publish(
    workflow_bindings,
    pulpcore_bindings,
    file_bindings,
    file_repo,
    file_content_unit_with_name_factory,
    task_plan_factory,
):
    """A TaskPlan that adds content then publishes the new version end-to-end."""
    repo = file_repo
    content_a = file_content_unit_with_name_factory(str(uuid.uuid4()))

    plan = task_plan_factory(
        steps=[
            {
                "index": 0,
                "task_name": "pulpcore.app.tasks.repository.add_and_remove",
                "task_kwargs": {
                    "repository_pk": _pk_from_href(repo.pulp_href),
                    "add_content_units": [_pk_from_href(content_a.pulp_href)],
                    "remove_content_units": [],
                },
                "reserved_resources": [repo.pulp_href],
            },
            {
                "index": 1,
                "task_name": "pulp_file.app.tasks.publish",
                "task_kwargs": {
                    "manifest": "PULP_MANIFEST",
                    # Resolved at dispatch time to the pk of the RepositoryVersion
                    # created by step 0.
                    "repository_version_pk": {"$prev_resource": "core.repositoryversion"},
                },
                "reserved_resources": [repo.pulp_href],
            },
        ],
    )

    finished = _wait_for_plan(workflow_bindings.WorkflowTaskPlansApi, plan.pulp_href)

    # ---- Plan-level assertions.
    assert finished.state == "completed", f"Plan state={finished.state!r} error={finished.error!r}"
    assert finished.error is None
    assert finished.started_at is not None
    assert finished.finished_at is not None
    assert finished.finished_at >= finished.started_at
    assert finished.current_step is None
    assert len(finished.steps) == 2

    step0, step1 = finished.steps[0], finished.steps[1]
    assert step0.dispatched_task is not None
    assert step1.dispatched_task is not None

    # ---- Each step's child task ran with the right resource.
    step0_task = pulpcore_bindings.TasksApi.read(step0.dispatched_task)
    step1_task = pulpcore_bindings.TasksApi.read(step1.dispatched_task)
    assert step0_task.state == "completed"
    assert step0_task.name == "pulpcore.app.tasks.repository.add_and_remove"
    assert repo.pulp_href in (step0_task.reserved_resources_record or [])
    assert step1_task.state == "completed"
    assert step1_task.name == "pulp_file.app.tasks.publish"
    assert repo.pulp_href in (step1_task.reserved_resources_record or [])

    # Both steps share the same parent task (the running execute_task_plan).
    assert step0_task.parent_task is not None
    assert step0_task.parent_task == step1_task.parent_task

    # Step 0 produced version 1.
    step0_versions = [h for h in (step0_task.created_resources or []) if "/versions/" in h]
    assert len(step0_versions) == 1
    version_href = step0_versions[0]
    assert version_href.endswith("/versions/1/")

    version = file_bindings.RepositoriesFileVersionsApi.read(version_href)
    assert version.content_summary.added.get("file.file", {}).get("count") == 1

    # ---- Repo's latest version is the new one.
    refreshed_repo = file_bindings.RepositoriesFileApi.read(repo.pulp_href)
    assert refreshed_repo.latest_version_href == version_href

    # ---- Step 1 produced a publication for that version.
    step1_publications = [h for h in (step1_task.created_resources or []) if "/publications/" in h]
    assert len(step1_publications) == 1
    publication = file_bindings.PublicationsFileApi.read(step1_publications[0])
    assert publication.repository_version == version_href
    assert publication.manifest == "PULP_MANIFEST"
