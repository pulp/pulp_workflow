import logging
import traceback

from django.db import transaction
from django.utils import timezone

from pulpcore.plugin.constants import TASK_STATES
from pulpcore.plugin.tasking import dispatch

from pulp_workflow.app.models import Workflow, WorkflowTask

_log = logging.getLogger(__name__)


def _workflow_resource(workflow_pk):
    """Resource string used to chain workflow steps via shared/exclusive locks."""
    return f"pulp_workflow:workflow:{workflow_pk}"


def execute_workflow(workflow_pk, next_index=0):
    """
    Run one step of a Workflow, then re-dispatch ourselves for the next step.

    Each invocation:
      1. If this is the first step, transitions the workflow to RUNNING (or
         exits if it was canceled before starting). Otherwise, checks the
         previous step's child task and fails the workflow if it did not
         COMPLETE.
      2. If there are no more tasks, marks the workflow COMPLETED and returns.
      3. Otherwise dispatches the next child task with a SHARED lock on the
         workflow's resource string, and dispatches a continuation of this
         function (``next_index + 1``) with an EXCLUSIVE lock on the same
         resource.

    Because pulpcore's tasking system will not grant the exclusive lock while
    the shared lock is held, the continuation cannot start until the child has
    finished. This avoids blocking a worker on a polling loop while the child
    runs, which would deadlock once concurrent workflows >= worker count.
    """
    workflow = Workflow.objects.get(pk=workflow_pk)

    if next_index == 0:
        # First step: honor a pre-start cancel and transition to RUNNING.
        with transaction.atomic():
            workflow = Workflow.objects.select_for_update().get(pk=workflow_pk)
            if workflow.state == TASK_STATES.CANCELED:
                _log.info("Workflow %s was canceled before starting.", workflow.name)
                return
            workflow.state = TASK_STATES.RUNNING
            workflow.started_at = timezone.now()
            workflow.save(update_fields=["state", "started_at", "pulp_last_updated"])
        _log.info("Workflow %s started.", workflow.name)
        prev_task = None
    else:
        # Continuation: inspect the previous step's child task.
        prev_wf_task = workflow.tasks.get(index=next_index - 1)
        prev_task = prev_wf_task.dispatched_task
        _log.debug(
            "Workflow %s step %d previous task ended in state %r.",
            workflow.name,
            next_index - 1,
            prev_task.state,
        )
        if prev_task.state != TASK_STATES.COMPLETED:
            _fail_workflow(
                workflow,
                prev_wf_task,
                description=f"Task ended in state {prev_task.state!r}.",
                child_error=prev_task.error,
            )
            return

    # If there is no task at next_index, the workflow is done.
    try:
        wf_task = workflow.tasks.get(index=next_index)
    except WorkflowTask.DoesNotExist:
        workflow.current_task = None
        workflow.state = TASK_STATES.COMPLETED
        workflow.finished_at = timezone.now()
        workflow.save(update_fields=["state", "finished_at", "current_task", "pulp_last_updated"])
        _log.info("Workflow %s completed.", workflow.name)
        return

    workflow.current_task = wf_task
    workflow.save(update_fields=["current_task", "pulp_last_updated"])

    # Dispatch the child task (SHARED on the workflow), then a continuation of
    # ourselves (EXCLUSIVE on the workflow) that will run after the child ends.
    resource = _workflow_resource(workflow_pk)
    try:
        resolved_args, resolved_kwargs = wf_task.materialize(prev_task)
        _log.debug(
            "Workflow %s dispatching step %d (%s).",
            workflow.name,
            next_index,
            wf_task.task_name,
        )
        child = dispatch(
            wf_task.task_name,
            args=resolved_args,
            kwargs=resolved_kwargs,
            exclusive_resources=wf_task.reserved_resources or None,
            shared_resources=[resource],
        )
    except Exception as exc:
        _log.exception("Workflow %s failed dispatching task %s", workflow.name, wf_task.index)
        _fail_workflow(workflow, wf_task, exc=exc)
        return

    wf_task.dispatched_task = child
    wf_task.save(update_fields=["dispatched_task", "pulp_last_updated"])

    dispatch(
        execute_workflow,
        kwargs={"workflow_pk": str(workflow_pk), "next_index": next_index + 1},
        exclusive_resources=[resource],
    )
    _log.debug("Workflow %s scheduled continuation for step %d.", workflow.name, next_index + 1)


def _fail_workflow(workflow, wf_task, exc=None, description=None, child_error=None):
    """Record a task failure on the workflow and transition it to FAILED."""
    workflow.state = TASK_STATES.FAILED
    workflow.finished_at = timezone.now()
    workflow.error = {
        "task_index": wf_task.index,
        "task_name": wf_task.task_name,
        "description": description or (str(exc) if exc else "Task failed."),
    }
    if exc is not None:
        workflow.error["traceback"] = traceback.format_exc()
    if child_error is not None:
        workflow.error["child_error"] = child_error
    workflow.save(update_fields=["state", "finished_at", "error", "pulp_last_updated"])
    _log.info(
        "Workflow %s failed at step %d (%s).", workflow.name, wf_task.index, wf_task.task_name
    )
