import logging
import time
import traceback

from django.db import transaction
from django.utils import timezone

from pulpcore.constants import TASK_FINAL_STATES, TASK_STATES
from pulpcore.plugin.tasking import dispatch

from pulp_workflow.app.models import Workflow

_log = logging.getLogger(__name__)

# How often to re-check a child task's state while waiting.
_CHILD_POLL_INTERVAL_SECONDS = 1.0

# Marker key used in a task's ``task_args`` / ``task_kwargs`` to reference a
# resource created by the previous task. The marker's value is a Django
# ``app_label.model`` string (e.g. ``"core.repositoryversion"``).
PREV_RESOURCE_MARKER = "$prev_resource"


def _resolve_prev_resource(model_key, prev_task):
    """Return the pk of the unique CreatedResource of type ``model_key``."""
    if prev_task is None:
        raise ValueError(
            f"{PREV_RESOURCE_MARKER!r} marker used in task 0; no previous task exists."
        )
    try:
        app_label, model = model_key.split(".")
    except (ValueError, AttributeError):
        raise ValueError(
            f"{PREV_RESOURCE_MARKER!r} value must be 'app_label.model', got {model_key!r}."
        )
    matches = [
        cr
        for cr in prev_task.created_resources.all().select_related("content_type")
        if cr.content_type.app_label == app_label and cr.content_type.model == model
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {model_key!r} created resource on previous task "
            f"(task {prev_task.pk}), found {len(matches)}."
        )
    return str(matches[0].object_id)


def _resolve_value(value, prev_task):
    """Walk ``value`` and replace any ``$prev_resource`` markers in place.

    A marker is a dict ``{"$prev_resource": "<app_label>.<model>"}``; it is
    replaced with the pk (as a string) of the unique ``CreatedResource`` of
    that type produced by ``prev_task``. Lists and dicts are walked recursively;
    all other values pass through unchanged.
    """
    if isinstance(value, dict) and PREV_RESOURCE_MARKER in value:
        if len(value) != 1:
            raise ValueError(
                f"A {PREV_RESOURCE_MARKER!r} marker dict must have exactly one key; "
                f"got {sorted(value)}."
            )
        return _resolve_prev_resource(value[PREV_RESOURCE_MARKER], prev_task)
    if isinstance(value, list):
        return [_resolve_value(item, prev_task) for item in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, prev_task) for k, v in value.items()}
    return value


def execute_workflow(workflow_pk):
    """
    Execute the tasks of a Workflow in order, dispatching each as a child task.

    This task is dispatched by a pulpcore ``TaskSchedule`` that is created when a
    ``Workflow`` is submitted via the API. For each task we call
    ``pulpcore.plugin.tasking.dispatch`` to create a real pulpcore Task (which gets
    ``parent_task`` set to the running ``execute_workflow`` task automatically),
    record it on the WorkflowTask via ``dispatched_task``, and wait for it to reach
    a final state before moving on. If a child task ends in any non-COMPLETED final
    state the workflow transitions to FAILED and remaining tasks are not dispatched.

    Args:
        workflow_pk (str): The primary key of the Workflow to execute.
    """
    with transaction.atomic():
        workflow = Workflow.objects.select_for_update().get(pk=workflow_pk)
        if workflow.state == TASK_STATES.CANCELED:
            _log.info(
                "Workflow %s was canceled before starting; skipping execution.",
                workflow.name,
            )
            return
        workflow.state = TASK_STATES.RUNNING
        workflow.started_at = timezone.now()
        workflow.save(update_fields=["state", "started_at", "pulp_last_updated"])

    prev_task = None
    for wf_task in workflow.tasks.all():
        workflow.current_task = wf_task
        workflow.save(update_fields=["current_task", "pulp_last_updated"])

        try:
            resolved_args = _resolve_value(wf_task.task_args, prev_task)
            resolved_kwargs = _resolve_value(wf_task.task_kwargs, prev_task)
            child = dispatch(
                wf_task.task_name,
                args=resolved_args,
                kwargs=resolved_kwargs,
                exclusive_resources=wf_task.reserved_resources or None,
            )
        except Exception as exc:
            _log.exception("Workflow %s failed dispatching task %s", workflow.name, wf_task.index)
            _fail_workflow(workflow, wf_task, exc=exc)
            return

        wf_task.dispatched_task = child
        wf_task.save(update_fields=["dispatched_task", "pulp_last_updated"])

        final_state = _wait_for_task(child)
        if final_state != TASK_STATES.COMPLETED:
            child.refresh_from_db()
            _fail_workflow(
                workflow,
                wf_task,
                description=f"Task ended in state {final_state!r}.",
                child_error=child.error,
            )
            return
        prev_task = child

    workflow.current_task = None
    workflow.state = TASK_STATES.COMPLETED
    workflow.finished_at = timezone.now()
    workflow.save(update_fields=["state", "finished_at", "current_task", "pulp_last_updated"])


def _wait_for_task(task):
    """Block until ``task`` reaches a final state and return that state."""
    while True:
        task.refresh_from_db()
        if task.state in TASK_FINAL_STATES:
            return task.state
        time.sleep(_CHILD_POLL_INTERVAL_SECONDS)


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
