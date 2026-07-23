import logging
import traceback
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from pulpcore.plugin.constants import TASK_FINAL_STATES, TASK_STATES
from pulpcore.plugin.models import Task, TaskGroup, TaskSchedule
from pulpcore.plugin.tasking import dispatch

from pulp_workflow.app.models import (
    TRANSITION_CALLBACK_TYPES,
    Workflow,
    WorkflowCallback,
    WorkflowRun,
    WorkflowRunCallback,
    WorkflowTask,
)

_log = logging.getLogger(__name__)


def _run_resource(workflow_run_pk):
    """Resource string used to chain a run's steps via shared/exclusive locks."""
    return f"pulp_workflow:run:{workflow_run_pk}"


def _run_is_live(run):
    """Return True while a task is actively driving ``run`` forward.

    Live means the run is still within the ``WORKER_TTL``-based grace period (its first step may
    not have dispatched yet) or its ``task_group`` still has a non-final task. Otherwise no worker
    is advancing it, so it has stalled and can be reclaimed instead of blocking the schedule. The
    grace period keys off ``WORKER_TTL`` so we don't reclaim before pulpcore fails a dead worker's
    tasks.
    """
    if timezone.now() - run.pulp_created < timedelta(seconds=settings.WORKER_TTL * 2):
        return True
    group = run.task_group
    if group is None:
        return False
    return group.tasks.exclude(state__in=TASK_FINAL_STATES).exists()


def start_workflow_run(workflow_pk):
    """Create a new :class:`WorkflowRun` and dispatch its first step.

    This is the task registered with pulpcore's ``TaskSchedule``. For a one-shot
    workflow it fires once; for a periodic workflow (``dispatch_interval`` set) it
    fires on every interval. A new run is only started if the workflow has no
    unfinished run, so overlapping runs of the same workflow are skipped. An
    unfinished run that has stalled (its worker died and no task is advancing it) is
    reclaimed as failed so it doesn't block the schedule forever.
    """
    with transaction.atomic():
        # Lock the workflow so concurrent schedule fires can't both start a run.
        workflow = Workflow.objects.select_for_update().get(pk=workflow_pk)
        # A stopped workflow has its schedule deleted; don't start a straggler run.
        if not TaskSchedule.objects.filter(name=f"pulp_workflow.workflow:{workflow.pk}").exists():
            _log.info("Workflow %s is stopped; skipping run.", workflow.name)
            return
        unfinished_runs = (
            WorkflowRun.objects.select_for_update(of=("self",))
            .filter(workflow=workflow)
            .exclude(state__in=TASK_FINAL_STATES)
            .select_related("task_group")
        )
        for prior_run in unfinished_runs:
            if _run_is_live(prior_run):
                _log.info("Workflow %s has an unfinished run; skipping.", workflow.name)
                return
            # No task is driving this run forward (its worker died before dispatching the first
            # step or mid-execution), so it would otherwise block this schedule indefinitely.
            # Reclaim it as failed so a new run can start.
            _log.warning("Reclaiming stalled run %s for workflow %s.", prior_run.pk, workflow.name)
            prior_run.state = TASK_STATES.FAILED
            prior_run.finished_at = timezone.now()
            prior_run.error = {"description": "Run stalled without an active task; reclaimed."}
            prior_run.save(update_fields=["state", "finished_at", "error", "pulp_last_updated"])
            _mark_task_group_dispatched(prior_run)
        task_group = TaskGroup.objects.create(
            description=f"Workflow run: {workflow.name}",
            pulp_domain=workflow.pulp_domain,
        )
        run = WorkflowRun.objects.create(
            workflow=workflow,
            pulp_domain=workflow.pulp_domain,
            task_group=task_group,
        )
    resource = _run_resource(run.pk)
    try:
        dispatch(
            execute_workflow,
            kwargs={"workflow_run_pk": str(run.pk)},
            exclusive_resources=[resource],
            task_group=task_group,
        )
    except Exception:
        _log.exception("Failed to dispatch first step for run %s; marking run failed.", run.pk)
        run.state = TASK_STATES.FAILED
        run.finished_at = timezone.now()
        run.error = {"description": "Failed to dispatch the workflow run's first step."}
        run.save(update_fields=["state", "finished_at", "error", "pulp_last_updated"])
        dispatch_workflow_callbacks(run, TASK_STATES.FAILED)
        _mark_task_group_dispatched(run)
        return
    _log.info("Started run %s for workflow %s.", run.pk, workflow.name)


def dispatch_workflow_callbacks(run, new_state):
    """Dispatch a ``run_callback`` task for each callback matching ``new_state``.

    Called whenever a WorkflowRun transitions to a new state. The mapping from state to callback
    types lives in ``TRANSITION_CALLBACK_TYPES``; states not in that map (e.g. ``waiting`` and
    ``canceled``) do not fire callbacks. Callbacks are attached to the workflow definition and fire
    for each run, joining the run's ``task_group``.

    Best-effort: a failure to dispatch any one callback is logged but does not bubble up so that
    one bad callback cannot break the run's own state transition.
    """
    types = TRANSITION_CALLBACK_TYPES.get(new_state)
    if not types:
        return
    callbacks = run.workflow.callbacks.filter(callback_type__in=types).select_related(
        "callback_service"
    )
    for wfcb in callbacks:
        try:
            child = dispatch(
                run_callback,
                kwargs={"workflow_callback_pk": str(wfcb.pk), "workflow_run_pk": str(run.pk)},
                task_group=run.task_group,
            )
            WorkflowRunCallback.objects.get_or_create(
                workflow_run=run,
                workflow_callback=wfcb,
                defaults={"dispatched_task": child},
            )
        except Exception:
            _log.exception(
                "Failed to dispatch callback %s for workflow run %s",
                wfcb.callback_service.name,
                run.pk,
            )
            continue


def run_callback(workflow_callback_pk, workflow_run_pk):
    """Pulp task that invokes a ``CallbackService`` for one ``WorkflowCallback``.

    The callback service runs as a subprocess on the worker host with the run's state and the
    workflow's name, pk, and labels exposed via ``PULP_WORKFLOW_*`` environment variables. Non-zero
    exit raises ``RuntimeError``, which marks this task as failed (visible via the run's
    ``WorkflowRunCallback.dispatched_task`` href on the WorkflowRun detail endpoint).
    """
    wfcb = WorkflowCallback.objects.select_related("workflow", "callback_service").get(
        pk=workflow_callback_pk
    )
    run = WorkflowRun.objects.select_related("workflow").get(pk=workflow_run_pk)
    if wfcb.workflow_id != run.workflow_id:
        raise RuntimeError(
            f"WorkflowCallback {workflow_callback_pk} (workflow {wfcb.workflow_id}) does not "
            f"belong to WorkflowRun {workflow_run_pk} (workflow {run.workflow_id})."
        )
    return wfcb.callback_service.run(run)


def execute_workflow(workflow_run_pk, next_index=0, prev_task_pk=None):
    """
    Run one step of a WorkflowRun, then re-dispatch ourselves for the next step.

    Each invocation:
      1. If this is the first step, transitions the run to RUNNING (or exits if it
         was canceled before starting). Otherwise, checks the previous step's child
         task and fails the run if it did not COMPLETE.
      2. If there are no more tasks, marks the run COMPLETED and returns.
      3. Otherwise dispatches the next child task with a SHARED lock on the run's
         resource string, and dispatches a continuation of this function
         (``next_index + 1``) with an EXCLUSIVE lock on the same resource, passing
         the child's pk forward as ``prev_task_pk``.

    Because pulpcore's tasking system will not grant the exclusive lock while
    the shared lock is held, the continuation cannot start until the child has
    finished. This avoids blocking a worker on a polling loop while the child
    runs, which would deadlock once concurrent runs >= worker count.
    """
    run = WorkflowRun.objects.select_related("workflow").get(pk=workflow_run_pk)
    workflow = run.workflow

    # Honor cancellation at any step. The run row may have been flipped to
    # CANCELED by a PATCH cancel, or by the post_save signal that propagates a
    # TaskGroup-level cancel.
    if run.state == TASK_STATES.CANCELED:
        _log.info("Run %s is canceled; stopping at step %d.", run.pk, next_index)
        _mark_task_group_dispatched(run)
        return

    if next_index == 0:
        # First step: re-check cancel under a row lock and transition to RUNNING.
        with transaction.atomic():
            run = WorkflowRun.objects.select_for_update().get(pk=workflow_run_pk)
            if run.state == TASK_STATES.CANCELED:
                _mark_task_group_dispatched(run)
                return
            run.state = TASK_STATES.RUNNING
            run.started_at = timezone.now()
            run.save(update_fields=["state", "started_at", "pulp_last_updated"])
        _log.info("Run %s started (workflow %s).", run.pk, workflow.name)
        dispatch_workflow_callbacks(run, TASK_STATES.RUNNING)
        prev_task = None
    else:
        # Continuation: inspect the previous step's child task.
        prev_wf_task = workflow.tasks.get(index=next_index - 1)
        prev_task = Task.objects.filter(pk=prev_task_pk).first() if prev_task_pk else None
        if prev_task is None:
            # The previously-dispatched ``core.Task`` may have been deleted (e.g. by
            # orphan_cleanup) between dispatch and this continuation.
            _fail_workflow(
                run,
                prev_wf_task,
                description="Previously dispatched task no longer exists.",
            )
            return
        _log.debug(
            "Run %s step %d previous task ended in state %r.",
            run.pk,
            next_index - 1,
            prev_task.state,
        )
        if prev_task.state != TASK_STATES.COMPLETED:
            _fail_workflow(
                run,
                prev_wf_task,
                description=f"Task ended in state {prev_task.state!r}.",
                child_error=prev_task.error,
            )
            return

    # If there is no task at next_index, the run is done.
    try:
        wf_task = workflow.tasks.get(index=next_index)
    except WorkflowTask.DoesNotExist:
        with transaction.atomic():
            run = WorkflowRun.objects.select_for_update().get(pk=workflow_run_pk)
            if run.state == TASK_STATES.CANCELED:
                _mark_task_group_dispatched(run)
                return
            run.current_task = None
            run.state = TASK_STATES.COMPLETED
            run.finished_at = timezone.now()
            run.save(update_fields=["state", "finished_at", "current_task", "pulp_last_updated"])
        _log.info("Run %s completed (workflow %s).", run.pk, workflow.name)
        dispatch_workflow_callbacks(run, TASK_STATES.COMPLETED)
        _mark_task_group_dispatched(run)
        return

    # Dispatch the child task (SHARED on the run), then a continuation of
    # ourselves (EXCLUSIVE on the run) that will run after the child ends.
    resource = _run_resource(workflow_run_pk)
    try:
        resolved_args, resolved_kwargs = wf_task.materialize(prev_task)
        # Re-check cancel under a row lock, then dispatch within the same
        # transaction so a concurrent PATCH cancel is either seen here (we skip)
        # or blocked until the child Task commits, so its on-commit
        # ``cancel_task_group`` cancels the child we just created.
        with transaction.atomic():
            run = (
                WorkflowRun.objects.select_for_update(of=("self",))
                .select_related("workflow", "task_group")
                .get(pk=workflow_run_pk)
            )
            if run.state == TASK_STATES.CANCELED:
                _log.info("Run %s is canceled; stopping at step %d.", run.pk, next_index)
                _mark_task_group_dispatched(run)
                return
            _log.debug(
                "Run %s dispatching step %d (%s).",
                run.pk,
                next_index,
                wf_task.task_name,
            )
            child = dispatch(
                wf_task.task_name,
                args=resolved_args,
                kwargs=resolved_kwargs,
                exclusive_resources=wf_task.reserved_resources or None,
                shared_resources=[resource],
                task_group=run.task_group,
            )
            run.current_task = child
            run.save(update_fields=["current_task", "pulp_last_updated"])
            dispatch(
                execute_workflow,
                kwargs={
                    "workflow_run_pk": str(workflow_run_pk),
                    "next_index": next_index + 1,
                    "prev_task_pk": str(child.pk),
                },
                exclusive_resources=[resource],
                task_group=run.task_group,
            )
    except Exception as exc:
        _log.exception("Run %s failed dispatching task %s", run.pk, wf_task.index)
        _fail_workflow(run, wf_task, exc=exc)
        return

    _log.debug("Run %s scheduled continuation for step %d.", run.pk, next_index + 1)


def _fail_workflow(run, wf_task, exc=None, description=None, child_error=None):
    """Record a task failure on the run and transition it to FAILED."""
    error = {
        "task_index": wf_task.index,
        "task_name": wf_task.task_name,
        "description": description or (str(exc) if exc else "Task failed."),
    }
    if exc is not None:
        error["traceback"] = traceback.format_exc()
    if child_error is not None:
        error["child_error"] = child_error
    with transaction.atomic():
        run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        if run.state == TASK_STATES.CANCELED:
            _mark_task_group_dispatched(run)
            return
        run.state = TASK_STATES.FAILED
        run.finished_at = timezone.now()
        run.error = error
        run.save(update_fields=["state", "finished_at", "error", "pulp_last_updated"])
    _log.info("Run %s failed at step %d (%s).", run.pk, wf_task.index, wf_task.task_name)
    dispatch_workflow_callbacks(run, TASK_STATES.FAILED)
    _mark_task_group_dispatched(run)


def _mark_task_group_dispatched(run):
    group = run.task_group
    if group is None or group.all_tasks_dispatched:
        return
    group.all_tasks_dispatched = True
    group.save(update_fields=["all_tasks_dispatched", "pulp_last_updated"])
