from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from pulpcore.plugin.constants import TASK_FINAL_STATES, TASK_STATES
from pulpcore.plugin.models import TaskGroup


@receiver(post_save, sender=TaskGroup)
def sync_workflow_state_on_task_group_dispatch(sender, instance, **kwargs):
    # Propagate a TaskGroup-level cancel (e.g. POST /task-groups/<pk>/cancel/) to the
    # owning WorkflowRun. ``cancel_task_group`` flips ``all_tasks_dispatched`` via a real
    # ``save()`` call (so this signal fires), whereas pulpcore's per-task cancel paths
    # use queryset .update() and would bypass a Task post_save handler.
    #
    # In the normal completion/failure path, the WorkflowRun row is moved to a terminal
    # state BEFORE we flip the group, so a non-terminal run attached to a group
    # that just had ``all_tasks_dispatched`` set to True can only mean the group was
    # canceled out-of-band. Callbacks are not currently supported for cancellation.
    if not instance.all_tasks_dispatched:
        return

    from pulp_workflow.app.models import WorkflowRun

    with transaction.atomic():
        runs = (
            WorkflowRun.objects.select_for_update()
            .filter(task_group_id=instance.pk)
            .exclude(state__in=TASK_FINAL_STATES)
        )
        for run in runs:
            run.state = TASK_STATES.CANCELED
            run.finished_at = timezone.now()
            run.save(update_fields=["state", "finished_at", "pulp_last_updated"])
