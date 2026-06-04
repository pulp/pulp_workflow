from unittest import mock

from pulpcore.plugin.constants import TASK_STATES

from pulp_workflow.app import tasks as tasks_module


def test_execute_workflow_fails_when_prev_dispatched_task_missing():
    """If a previous step's child Task was deleted (SET_NULL), fail clearly.

    ``WorkflowTask.dispatched_task`` is nullable with ``on_delete=SET_NULL``,
    so the underlying ``core.Task`` may disappear (e.g. via orphan_cleanup)
    between dispatch and the continuation. The workflow should be transitioned
    to FAILED with a clear description instead of raising ``AttributeError``.
    """
    workflow = mock.Mock(name="workflow")
    prev_wf_task = mock.Mock(name="prev_wf_task")
    prev_wf_task.dispatched_task = None
    workflow.tasks.get.return_value = prev_wf_task

    with (
        mock.patch.object(
            tasks_module.Workflow.objects, "get", return_value=workflow
        ) as get_workflow,
        mock.patch.object(tasks_module, "_fail_workflow") as fail_workflow,
        mock.patch.object(tasks_module, "dispatch") as dispatch_task,
    ):
        tasks_module.execute_workflow("pk-sentinel", next_index=1)

    get_workflow.assert_called_once_with(pk="pk-sentinel")
    workflow.tasks.get.assert_called_once_with(index=0)
    fail_workflow.assert_called_once_with(
        workflow,
        prev_wf_task,
        description="Previously dispatched task no longer exists.",
    )
    dispatch_task.assert_not_called()


def test_execute_workflow_returns_early_when_canceled_continuation():
    """A continuation that observes ``state == CANCELED`` exits without dispatching."""
    workflow = mock.Mock(name="workflow", state=TASK_STATES.CANCELED)

    with (
        mock.patch.object(tasks_module.Workflow.objects, "get", return_value=workflow),
        mock.patch.object(tasks_module, "_mark_task_group_dispatched") as mark_dispatched,
        mock.patch.object(tasks_module, "dispatch") as dispatch_task,
        mock.patch.object(tasks_module, "_fail_workflow") as fail_workflow,
    ):
        tasks_module.execute_workflow("pk-sentinel", next_index=3)

    mark_dispatched.assert_called_once_with(workflow)
    dispatch_task.assert_not_called()
    fail_workflow.assert_not_called()
    # The continuation must not inspect or mutate previous-step state once canceled.
    workflow.tasks.get.assert_not_called()


def test_execute_workflow_returns_early_when_canceled_first_step():
    """First-step cancel still short-circuits before any RUNNING transition."""
    workflow = mock.Mock(name="workflow", state=TASK_STATES.CANCELED)

    with (
        mock.patch.object(tasks_module.Workflow.objects, "get", return_value=workflow),
        mock.patch.object(tasks_module, "_mark_task_group_dispatched") as mark_dispatched,
        mock.patch.object(tasks_module, "dispatch") as dispatch_task,
    ):
        tasks_module.execute_workflow("pk-sentinel", next_index=0)

    mark_dispatched.assert_called_once_with(workflow)
    dispatch_task.assert_not_called()
    # The workflow must not be flipped to RUNNING.
    workflow.save.assert_not_called()
