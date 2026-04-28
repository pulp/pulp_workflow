from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

# EncryptedJSONField is not yet re-exported through pulpcore.plugin; mirror
# pulpcore's own usage on Task.enc_args / TaskSchedule.task_args.
from pulpcore.app.models.fields import EncryptedJSONField  # noqa: TID251
from pulpcore.constants import TASK_CHOICES, TASK_STATES
from pulpcore.plugin.models import BaseModel
from pulpcore.plugin.util import get_domain_pk


class TaskPlan(BaseModel):
    """
    A named, ordered pipeline of tasks executed sequentially.

    Fields:
        name (models.TextField): Unique name of the plan.
        state (models.TextField): Current state of the plan, drawn from
            ``pulpcore.constants.TASK_STATES``.
        start_time (models.DateTimeField): When the plan should start executing.
            A pulpcore TaskSchedule is created at this time to dispatch the
            ``execute_task_plan`` task.
        started_at (models.DateTimeField): When the first step was dispatched.
        finished_at (models.DateTimeField): When the plan reached a terminal state.
        error (models.JSONField): Fatal error info, populated from a failing step.

    Relations:
        pulp_domain (models.ForeignKey): Domain the plan belongs to.
        current_step (models.ForeignKey): The step currently in progress (if any).
    """

    name = models.TextField(unique=True)
    state = models.TextField(choices=TASK_CHOICES, default=TASK_STATES.WAITING)
    start_time = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    error = models.JSONField(null=True)

    pulp_domain = models.ForeignKey("core.Domain", default=get_domain_pk, on_delete=models.CASCADE)
    current_step = models.ForeignKey(
        "TaskPlanStep",
        null=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )

    def __str__(self):
        return "TaskPlan: {name} [{state}]".format(name=self.name, state=self.state)

    class Meta:
        permissions = [
            ("manage_roles_taskplan", "Can manage role assignments on task plans"),
        ]


class TaskPlanStep(BaseModel):
    """
    A single step within a TaskPlan.

    Fields:
        index (models.PositiveIntegerField): Execution order within the plan.
        task_name (models.TextField): Dotted Python path of the task to dispatch.
        task_args (EncryptedJSONField): Positional args for the task.
        task_kwargs (EncryptedJSONField): Keyword args for the task.
        reserved_resources (ArrayField): Resources to reserve when dispatching.

    Relations:
        plan (models.ForeignKey): The TaskPlan this step belongs to.
        dispatched_task (models.ForeignKey): The Task created when this step ran.
    """

    plan = models.ForeignKey(TaskPlan, related_name="steps", on_delete=models.CASCADE)
    index = models.PositiveIntegerField()
    task_name = models.TextField()
    task_args = EncryptedJSONField(default=list)
    task_kwargs = EncryptedJSONField(default=dict)
    reserved_resources = ArrayField(models.TextField(), null=True)
    dispatched_task = models.ForeignKey(
        "core.Task",
        null=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )

    def __str__(self):
        return "TaskPlanStep: {plan}[{index}] {task_name}".format(
            plan=self.plan.name, index=self.index, task_name=self.task_name
        )

    class Meta:
        unique_together = ("plan", "index")
        ordering = ("index",)
