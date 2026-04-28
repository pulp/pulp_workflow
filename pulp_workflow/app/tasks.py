import logging
import time
import traceback

from django.utils import timezone

from pulpcore.constants import TASK_FINAL_STATES, TASK_STATES
from pulpcore.plugin.tasking import dispatch

from pulp_workflow.app.models import TaskPlan

_log = logging.getLogger(__name__)

# How often to re-check a child task's state while waiting.
_CHILD_POLL_INTERVAL_SECONDS = 1.0

# Marker key used in a step's ``task_args`` / ``task_kwargs`` to reference a
# resource created by the previous step. The marker's value is a Django
# ``app_label.model`` string (e.g. ``"core.repositoryversion"``).
PREV_RESOURCE_MARKER = "$prev_resource"


def _resolve_prev_resource(model_key, prev_task):
    """Return the pk of the unique CreatedResource of type ``model_key``."""
    if prev_task is None:
        raise ValueError(
            f"{PREV_RESOURCE_MARKER!r} marker used in step 0; no previous step exists."
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
            f"Expected exactly one {model_key!r} created resource on previous step "
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


def execute_task_plan(task_plan_pk):
    """
    Execute the steps of a TaskPlan in order, dispatching each step as a child task.

    This task is dispatched by a pulpcore ``TaskSchedule`` that is created when a
    ``TaskPlan`` is submitted via the API. For each step we call
    ``pulpcore.plugin.tasking.dispatch`` to create a real pulpcore Task (which gets
    ``parent_task`` set to the running ``execute_task_plan`` task automatically),
    record it on the step via ``dispatched_task``, and wait for it to reach a final
    state before moving on. If a child task ends in any non-COMPLETED final state
    the plan transitions to FAILED and remaining steps are not dispatched.

    Args:
        task_plan_pk (str): The primary key of the TaskPlan to execute.
    """
    plan = TaskPlan.objects.get(pk=task_plan_pk)

    plan.state = TASK_STATES.RUNNING
    plan.started_at = timezone.now()
    plan.save(update_fields=["state", "started_at", "pulp_last_updated"])

    prev_task = None
    for step in plan.steps.all():
        plan.current_step = step
        plan.save(update_fields=["current_step", "pulp_last_updated"])

        try:
            resolved_args = _resolve_value(step.task_args, prev_task)
            resolved_kwargs = _resolve_value(step.task_kwargs, prev_task)
            child = dispatch(
                step.task_name,
                args=resolved_args,
                kwargs=resolved_kwargs,
                exclusive_resources=step.reserved_resources or None,
            )
        except Exception as exc:
            _log.exception("TaskPlan %s failed dispatching step %s", plan.name, step.index)
            _fail_plan(plan, step, exc=exc)
            return

        step.dispatched_task = child
        step.save(update_fields=["dispatched_task", "pulp_last_updated"])

        final_state = _wait_for_task(child)
        if final_state != TASK_STATES.COMPLETED:
            child.refresh_from_db()
            _fail_plan(
                plan,
                step,
                description=f"Step task ended in state {final_state!r}.",
                child_error=child.error,
            )
            return
        prev_task = child

    plan.current_step = None
    plan.state = TASK_STATES.COMPLETED
    plan.finished_at = timezone.now()
    plan.save(update_fields=["state", "finished_at", "current_step", "pulp_last_updated"])


def _wait_for_task(task):
    """Block until ``task`` reaches a final state and return that state."""
    while True:
        task.refresh_from_db()
        if task.state in TASK_FINAL_STATES:
            return task.state
        time.sleep(_CHILD_POLL_INTERVAL_SECONDS)


def _fail_plan(plan, step, exc=None, description=None, child_error=None):
    """Record a step failure on the plan and transition it to FAILED."""
    plan.state = TASK_STATES.FAILED
    plan.finished_at = timezone.now()
    plan.error = {
        "step_index": step.index,
        "task_name": step.task_name,
        "description": description or (str(exc) if exc else "Step failed."),
    }
    if exc is not None:
        plan.error["traceback"] = traceback.format_exc()
    if child_error is not None:
        plan.error["child_error"] = child_error
    plan.save(update_fields=["state", "finished_at", "error", "pulp_last_updated"])
