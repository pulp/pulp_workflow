import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.parallel
def test_create_workflow(workflow_bindings, workflow_factory):
    """Test creating a Workflow with multiple tasks."""
    name = str(uuid.uuid4())
    workflow = workflow_factory(
        name=name,
        tasks=[
            {
                "task_name": "pulpcore.app.tasks.orphan_cleanup",
                "task_kwargs": [{"kwarg_key": "orphan_protection_time", "value": 0}],
            },
            {
                "task_name": "pulpcore.app.tasks.orphan_cleanup",
            },
        ],
    )
    assert workflow.name == name
    assert workflow.pulp_href is not None
    assert workflow.state == "waiting"
    assert workflow.started_at is None
    assert workflow.finished_at is None
    assert workflow.error is None
    assert workflow.current_task is None
    assert len(workflow.tasks) == 2
    assert workflow.tasks[0].index == 0
    assert workflow.tasks[0].task_name == "pulpcore.app.tasks.orphan_cleanup"
    assert workflow.tasks[0].dispatched_task is None
    assert workflow.tasks[1].index == 1


@pytest.mark.parallel
def test_create_workflow_minimal(workflow_bindings, workflow_factory):
    """A single-task workflow with default args/kwargs/reserved_resources is accepted."""
    workflow = workflow_factory()
    assert len(workflow.tasks) == 1
    wf_task = workflow.tasks[0]
    assert wf_task.reserved_resources is None


@pytest.mark.parallel
def test_create_workflow_no_tasks_fails(workflow_bindings):
    """A Workflow must have at least one task."""
    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.WorkflowsApi.create({"name": str(uuid.uuid4()), "tasks": []})
    assert exc.value.status == 400


@pytest.mark.parallel
def test_create_duplicate_workflow_name_fails(workflow_bindings, workflow_factory):
    """Creating a workflow with a name already in use fails."""
    name = str(uuid.uuid4())
    workflow_factory(name=name)

    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.WorkflowsApi.create(
            {
                "name": name,
                "tasks": [
                    {"task_name": "pulpcore.app.tasks.orphan_cleanup"},
                ],
            }
        )
    assert exc.value.status == 400


@pytest.mark.parallel
def test_read_workflow(workflow_bindings, workflow_factory):
    """A created Workflow can be retrieved by href."""
    workflow = workflow_factory()
    fetched = workflow_bindings.WorkflowsApi.read(workflow.pulp_href)
    assert fetched.pulp_href == workflow.pulp_href
    assert fetched.name == workflow.name
    assert len(fetched.tasks) == 1


@pytest.mark.parallel
def test_list_workflows(workflow_bindings, workflow_factory):
    """Listing and filtering Workflows."""
    name = str(uuid.uuid4())
    workflow_factory(name=name)

    results = workflow_bindings.WorkflowsApi.list(name=name)
    assert results.count == 1
    assert results.results[0].name == name

    results = workflow_bindings.WorkflowsApi.list(state="waiting")
    assert results.count >= 1


@pytest.mark.parallel
def test_list_workflows_extra_filters(workflow_bindings, workflow_factory):
    """The new filters from BaseFilterSet (pulp_href__in, name__contains, q) work."""
    prefix = f"filt-{uuid.uuid4().hex[:8]}"
    a = workflow_factory(name=f"{prefix}-a")
    b = workflow_factory(name=f"{prefix}-b")

    # name__contains
    results = workflow_bindings.WorkflowsApi.list(name__contains=prefix)
    assert {w.name for w in results.results} == {a.name, b.name}

    # pulp_href__in
    results = workflow_bindings.WorkflowsApi.list(pulp_href__in=[a.pulp_href])
    assert [w.pulp_href for w in results.results] == [a.pulp_href]

    # q expression filter (provided by BaseFilterSet)
    results = workflow_bindings.WorkflowsApi.list(q=f"name__contains={prefix} AND state=waiting")
    assert {w.name for w in results.results} == {a.name, b.name}


@pytest.mark.parallel
def test_delete_workflow_not_supported(workflow_bindings):
    """The destroy endpoint has been removed; bindings do not expose ``delete``."""
    api = workflow_bindings.WorkflowsApi
    assert not hasattr(api, "delete")


@pytest.mark.parallel
def test_cancel_workflow(workflow_bindings, workflow_factory):
    """A waiting Workflow can be canceled and reaches the canceled state."""
    # Schedule the workflow far enough in the future that it cannot start before
    # the cancel request is processed.
    from datetime import datetime, timedelta, timezone

    start_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    workflow = workflow_factory(start_time=start_time)
    assert workflow.state == "waiting"

    canceled = workflow_bindings.WorkflowsApi.workflows_cancel(
        workflow.pulp_href, {"state": "canceled"}
    )
    assert canceled.state == "canceled"
    assert canceled.finished_at is not None

    # The workflow is still readable in the canceled state.
    fetched = workflow_bindings.WorkflowsApi.read(workflow.pulp_href)
    assert fetched.state == "canceled"


@pytest.mark.parallel
def test_cancel_workflow_invalid_state_value(workflow_bindings, workflow_factory):
    """Only 'canceled' is accepted as the target state.

    The cancel body's ``state`` is a single-choice enum, so the bindings reject any
    other value client-side via pydantic validation before the request is sent.
    """
    from datetime import datetime, timedelta, timezone

    from pydantic import ValidationError

    start_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    workflow = workflow_factory(start_time=start_time)

    with pytest.raises(ValidationError):
        workflow_bindings.WorkflowsApi.workflows_cancel(workflow.pulp_href, {"state": "completed"})


@pytest.mark.parallel
def test_workflow_update_not_supported(workflow_bindings):
    """Workflows are immutable; the bindings expose only the cancel (partial_update)."""
    api = workflow_bindings.WorkflowsApi
    assert not hasattr(api, "update")


@pytest.mark.parallel
def test_task_args_and_kwargs_not_exposed(workflow_bindings, workflow_factory):
    """Arg ``value`` fields are write-only and never returned in responses."""
    # Schedule far in the future so the dummy args never actually dispatch.
    start_time = datetime.now(timezone.utc) + timedelta(days=365)
    workflow = workflow_factory(
        start_time=start_time.isoformat(),
        tasks=[
            {
                "task_name": "pulpcore.app.tasks.orphan_cleanup",
                "task_args": [{"value": "secret-positional"}],
                "task_kwargs": [{"kwarg_key": "secret_keyword", "value": "s3cr3t"}],
            },
        ],
    )
    fetched = workflow_bindings.WorkflowsApi.read(workflow.pulp_href)
    raw = fetched.to_dict()["tasks"][0]
    for arg in raw.get("task_args") or []:
        assert "value" not in arg
    for kw in raw.get("task_kwargs") or []:
        assert "value" not in kw
