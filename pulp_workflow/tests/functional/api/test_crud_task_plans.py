import uuid

import pytest


@pytest.mark.parallel
def test_create_task_plan(workflow_bindings, task_plan_factory):
    """Test creating a TaskPlan with multiple steps."""
    name = str(uuid.uuid4())
    plan = task_plan_factory(
        name=name,
        steps=[
            {
                "index": 0,
                "task_name": "pulpcore.app.tasks.orphan_cleanup",
                "task_kwargs": {"orphan_protection_time": 0},
            },
            {
                "index": 1,
                "task_name": "pulpcore.app.tasks.orphan_cleanup",
            },
        ],
    )
    assert plan.name == name
    assert plan.pulp_href is not None
    assert plan.state == "waiting"
    assert plan.started_at is None
    assert plan.finished_at is None
    assert plan.error is None
    assert plan.current_step is None
    assert len(plan.steps) == 2
    assert plan.steps[0].index == 0
    assert plan.steps[0].task_name == "pulpcore.app.tasks.orphan_cleanup"
    # task_args/task_kwargs are write-only and must not be exposed in responses.
    assert not hasattr(plan.steps[0], "task_kwargs") or plan.steps[0].task_kwargs is None
    assert not hasattr(plan.steps[0], "task_args") or plan.steps[0].task_args is None
    assert plan.steps[0].dispatched_task is None
    assert plan.steps[1].index == 1


@pytest.mark.parallel
def test_create_task_plan_minimal(workflow_bindings, task_plan_factory):
    """A single-step plan with default args/kwargs/reserved_resources is accepted."""
    plan = task_plan_factory()
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.reserved_resources is None


@pytest.mark.parallel
def test_create_task_plan_no_steps_fails(workflow_bindings):
    """A TaskPlan must have at least one step."""
    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.WorkflowTaskPlansApi.create({"name": str(uuid.uuid4()), "steps": []})
    assert exc.value.status == 400


@pytest.mark.parallel
def test_create_task_plan_duplicate_step_index_fails(workflow_bindings):
    """Step indexes must be unique within a plan."""
    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.WorkflowTaskPlansApi.create(
            {
                "name": str(uuid.uuid4()),
                "steps": [
                    {"index": 0, "task_name": "pulpcore.app.tasks.orphan_cleanup"},
                    {"index": 0, "task_name": "pulpcore.app.tasks.orphan_cleanup"},
                ],
            }
        )
    assert exc.value.status == 400


@pytest.mark.parallel
def test_create_duplicate_plan_name_fails(workflow_bindings, task_plan_factory):
    """Creating a plan with a name already in use fails."""
    name = str(uuid.uuid4())
    task_plan_factory(name=name)

    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.WorkflowTaskPlansApi.create(
            {
                "name": name,
                "steps": [
                    {"index": 0, "task_name": "pulpcore.app.tasks.orphan_cleanup"},
                ],
            }
        )
    assert exc.value.status == 400


@pytest.mark.parallel
def test_read_task_plan(workflow_bindings, task_plan_factory):
    """A created TaskPlan can be retrieved by href."""
    plan = task_plan_factory()
    fetched = workflow_bindings.WorkflowTaskPlansApi.read(plan.pulp_href)
    assert fetched.pulp_href == plan.pulp_href
    assert fetched.name == plan.name
    assert len(fetched.steps) == 1


@pytest.mark.parallel
def test_list_task_plans(workflow_bindings, task_plan_factory):
    """Listing and filtering TaskPlans."""
    name = str(uuid.uuid4())
    task_plan_factory(name=name)

    results = workflow_bindings.WorkflowTaskPlansApi.list(name=name)
    assert results.count == 1
    assert results.results[0].name == name

    results = workflow_bindings.WorkflowTaskPlansApi.list(state="waiting")
    assert results.count >= 1


@pytest.mark.parallel
def test_delete_task_plan(workflow_bindings):
    """A TaskPlan can be deleted; subsequent reads 404."""
    plan = workflow_bindings.WorkflowTaskPlansApi.create(
        {
            "name": str(uuid.uuid4()),
            "steps": [
                {"index": 0, "task_name": "pulpcore.app.tasks.orphan_cleanup"},
            ],
        }
    )
    workflow_bindings.WorkflowTaskPlansApi.delete(plan.pulp_href)

    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.WorkflowTaskPlansApi.read(plan.pulp_href)
    assert exc.value.status == 404


@pytest.mark.parallel
def test_task_plan_update_not_supported(workflow_bindings):
    """TaskPlans are immutable; the bindings do not expose update/partial_update."""
    api = workflow_bindings.WorkflowTaskPlansApi
    assert not hasattr(api, "update")
    assert not hasattr(api, "partial_update")


@pytest.mark.parallel
def test_task_args_and_kwargs_not_exposed(workflow_bindings, task_plan_factory):
    """task_args / task_kwargs are write-only and never returned in responses."""
    plan = task_plan_factory(
        steps=[
            {
                "index": 0,
                "task_name": "pulpcore.app.tasks.orphan_cleanup",
                "task_args": ["secret-positional"],
                "task_kwargs": {"secret_keyword": "s3cr3t"},
            },
        ],
    )
    fetched = workflow_bindings.WorkflowTaskPlansApi.read(plan.pulp_href)
    raw = fetched.to_dict()["steps"][0]
    assert "task_args" not in raw
    assert "task_kwargs" not in raw
