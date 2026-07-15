from unittest import mock

from pulpcore.plugin.constants import TASK_STATES

from pulp_workflow.app import tasks as tasks_module


def test_execute_workflow_fails_when_prev_dispatched_task_missing():
    """If a previous step's child Task was deleted (SET_NULL), fail clearly.

    The child ``core.Task`` may disappear (e.g. via orphan_cleanup) between dispatch
    and the continuation. The run should transition to FAILED with a clear description
    instead of raising ``AttributeError``.
    """
    run = mock.Mock(name="run", state=TASK_STATES.RUNNING)
    prev_wf_task = mock.Mock(name="prev_wf_task")
    run.workflow.tasks.get.return_value = prev_wf_task

    with (
        mock.patch.object(tasks_module.WorkflowRun, "objects") as run_objects,
        mock.patch.object(tasks_module.Task.objects, "filter") as task_filter,
        mock.patch.object(tasks_module, "_fail_workflow") as fail_workflow,
        mock.patch.object(tasks_module, "dispatch") as dispatch_task,
    ):
        run_objects.select_related.return_value.get.return_value = run
        task_filter.return_value.first.return_value = None
        tasks_module.execute_workflow("run-pk", next_index=1, prev_task_pk="gone")

    run_objects.select_related.return_value.get.assert_called_once_with(pk="run-pk")
    run.workflow.tasks.get.assert_called_once_with(index=0)
    fail_workflow.assert_called_once_with(
        run,
        prev_wf_task,
        description="Previously dispatched task no longer exists.",
    )
    dispatch_task.assert_not_called()


def test_execute_workflow_returns_early_when_canceled_continuation():
    """A continuation that observes ``state == CANCELED`` exits without dispatching."""
    run = mock.Mock(name="run", state=TASK_STATES.CANCELED)

    with (
        mock.patch.object(tasks_module.WorkflowRun, "objects") as run_objects,
        mock.patch.object(tasks_module, "_mark_task_group_dispatched") as mark_dispatched,
        mock.patch.object(tasks_module, "dispatch") as dispatch_task,
        mock.patch.object(tasks_module, "_fail_workflow") as fail_workflow,
    ):
        run_objects.select_related.return_value.get.return_value = run
        tasks_module.execute_workflow("run-pk", next_index=3, prev_task_pk="whatever")

    mark_dispatched.assert_called_once_with(run)
    dispatch_task.assert_not_called()
    fail_workflow.assert_not_called()
    # The continuation must not inspect or mutate previous-step state once canceled.
    run.workflow.tasks.get.assert_not_called()


def test_execute_workflow_returns_early_when_canceled_first_step():
    """First-step cancel still short-circuits before any RUNNING transition."""
    run = mock.Mock(name="run", state=TASK_STATES.CANCELED)

    with (
        mock.patch.object(tasks_module.WorkflowRun, "objects") as run_objects,
        mock.patch.object(tasks_module, "_mark_task_group_dispatched") as mark_dispatched,
        mock.patch.object(tasks_module, "dispatch") as dispatch_task,
    ):
        run_objects.select_related.return_value.get.return_value = run
        tasks_module.execute_workflow("run-pk", next_index=0)

    mark_dispatched.assert_called_once_with(run)
    dispatch_task.assert_not_called()
    # The run must not be flipped to RUNNING.
    run.save.assert_not_called()


def test_start_workflow_run_creates_run_and_dispatches_first_step():
    """``start_workflow_run`` creates a run + task group and dispatches step 0."""
    workflow = mock.Mock(name="workflow")
    workflow.name = "wf"
    run = mock.Mock(name="run")
    run.pk = "run-pk"
    task_group = mock.Mock(name="task_group")

    with (
        mock.patch.object(tasks_module.transaction, "atomic"),
        mock.patch.object(tasks_module.Workflow.objects, "select_for_update") as select_for_update,
        mock.patch.object(tasks_module.TaskSchedule, "objects") as schedule_objects,
        mock.patch.object(tasks_module.WorkflowRun, "objects") as run_objects,
        mock.patch.object(
            tasks_module.TaskGroup.objects, "create", return_value=task_group
        ) as create_group,
        mock.patch.object(tasks_module, "dispatch") as dispatch_task,
    ):
        select_for_update.return_value.get.return_value = workflow
        schedule_objects.filter.return_value.exists.return_value = True
        run_objects.filter.return_value.exclude.return_value.exists.return_value = False
        run_objects.create.return_value = run
        tasks_module.start_workflow_run("wf-pk")

    create_group.assert_called_once()
    run_objects.create.assert_called_once_with(
        workflow=workflow,
        pulp_domain=workflow.pulp_domain,
        task_group=task_group,
    )
    dispatch_task.assert_called_once()
    _, kwargs = dispatch_task.call_args
    assert kwargs["kwargs"] == {"workflow_run_pk": "run-pk"}
    assert kwargs["task_group"] is task_group


def test_execute_workflow_skips_dispatch_when_canceled_under_lock():
    """A cancel that commits after the top-of-task check is caught by the locked
    re-check, so no child task is dispatched.

    The initial (unlocked) read sees a RUNNING run, but by the time we re-read the
    run under ``select_for_update`` a concurrent PATCH cancel has flipped it to
    CANCELED. The step must abort before dispatching the child so a task cannot
    escape ``cancel_task_group``.
    """
    running_run = mock.Mock(name="running_run", state=TASK_STATES.RUNNING)
    wf_task = mock.Mock(name="wf_task")
    wf_task.materialize.return_value = ((), {})
    running_run.workflow.tasks.get.return_value = wf_task

    prev_task = mock.Mock(name="prev_task", state=TASK_STATES.COMPLETED)
    canceled_run = mock.Mock(name="canceled_run", state=TASK_STATES.CANCELED)

    with (
        mock.patch.object(tasks_module.transaction, "atomic"),
        mock.patch.object(tasks_module.WorkflowRun, "objects") as run_objects,
        mock.patch.object(tasks_module.Task.objects, "filter") as task_filter,
        mock.patch.object(tasks_module, "_mark_task_group_dispatched") as mark_dispatched,
        mock.patch.object(tasks_module, "dispatch") as dispatch_task,
        mock.patch.object(tasks_module, "_fail_workflow") as fail_workflow,
    ):
        run_objects.select_related.return_value.get.return_value = running_run
        locked = run_objects.select_for_update.return_value.select_related.return_value
        locked.get.return_value = canceled_run
        task_filter.return_value.first.return_value = prev_task
        tasks_module.execute_workflow("run-pk", next_index=1, prev_task_pk="prev-pk")

    dispatch_task.assert_not_called()
    fail_workflow.assert_not_called()
    mark_dispatched.assert_called_once_with(canceled_run)
