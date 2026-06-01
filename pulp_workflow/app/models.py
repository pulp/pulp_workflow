from django.contrib.postgres.fields import ArrayField, HStoreField
from django.db import models
from django.utils import timezone

from pulpcore.plugin.constants import TASK_CHOICES, TASK_STATES
from pulpcore.plugin.models import BaseModel, EncryptedJSONField
from pulpcore.plugin.util import get_domain_pk


class Workflow(BaseModel):
    """
    A named, ordered pipeline of tasks executed sequentially.

    Fields:
        name (models.TextField): Unique name of the workflow.
        pulp_labels (HStoreField): Dictionary of string values.
        state (models.TextField): Current state of the workflow, drawn from
            ``pulpcore.constants.TASK_STATES``.
        start_time (models.DateTimeField): When the workflow should start executing.
            A pulpcore TaskSchedule is created at this time to dispatch the
            ``execute_workflow`` task.
        started_at (models.DateTimeField): When the first task was dispatched.
        finished_at (models.DateTimeField): When the workflow reached a terminal state.
        error (models.JSONField): Fatal error info, populated from a failing task.

    Relations:
        pulp_domain (models.ForeignKey): Domain the workflow belongs to.
        current_task (models.ForeignKey): The task currently in progress (if any).
        task_group (models.ForeignKey): The pulpcore TaskGroup for all tasks of this workflow.
    """

    name = models.TextField(unique=True)
    pulp_labels = HStoreField(default=dict)
    state = models.TextField(choices=TASK_CHOICES, default=TASK_STATES.WAITING)
    start_time = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    error = models.JSONField(null=True)

    pulp_domain = models.ForeignKey("core.Domain", default=get_domain_pk, on_delete=models.CASCADE)
    current_task = models.ForeignKey(
        "WorkflowTask",
        null=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    task_group = models.ForeignKey(
        "core.TaskGroup",
        null=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )

    def __str__(self):
        return "Workflow: {name} [{state}]".format(name=self.name, state=self.state)

    class Meta:
        default_permissions = ("add", "change", "view")
        permissions = [
            ("manage_roles_workflow", "Can manage role assignments on workflows"),
        ]


class WorkflowTask(BaseModel):
    """A single task within a Workflow.

    Positional and keyword args are stored in the related ``task_args`` and
    ``task_kwargs`` tables (see ``_WorkflowTaskArgBase``).
    """

    workflow = models.ForeignKey(Workflow, related_name="tasks", on_delete=models.CASCADE)
    index = models.PositiveIntegerField()
    task_name = models.TextField()
    reserved_resources = ArrayField(models.TextField(), null=True)
    dispatched_task = models.ForeignKey(
        "core.Task",
        null=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )

    def __str__(self):
        return f"WorkflowTask: {self.workflow.name}[{self.index}] {self.task_name}"

    def materialize(self, prev_task):
        """Return ``(args, kwargs)`` for dispatching this task.

        Dynamic rows (those with a ``content_type`` set) are resolved against
        ``prev_task``'s ``created_resources`` by ``content_type``. Positional
        ``arg_index`` values are assigned at write time from the order of the
        ``task_args`` list and are contiguous from 0.
        """
        positional = self.task_args.select_related("content_type")
        keyword = self.task_kwargs.select_related("content_type")
        args = [a.resolve(prev_task) for a in sorted(positional, key=lambda a: a.arg_index)]
        kwargs = {kw.kwarg_key: kw.resolve(prev_task) for kw in keyword}
        return args, kwargs

    class Meta:
        unique_together = ("workflow", "index")
        ordering = ("index",)


class _WorkflowTaskArgBase(BaseModel):
    """Abstract base for a positional or keyword arg of a ``WorkflowTask``.

    A row is either *static* (``content_type`` is null; pass ``value`` through)
    or *dynamic* (``content_type`` is set; resolve to the pk of the previous
    task's unique created resource of that type).
    """

    value = EncryptedJSONField(null=True)
    content_type = models.ForeignKey(
        "contenttypes.ContentType",
        null=True,
        on_delete=models.PROTECT,
    )

    class Meta:
        abstract = True

    def resolve(self, prev_task):
        if self.content_type_id is None:
            return self.value
        if prev_task is None:
            raise ValueError("Dynamic workflow arg used in task 0; no previous task exists.")
        matches = [
            cr
            for cr in prev_task.created_resources.all()
            if cr.content_type_id == self.content_type_id
        ]
        if len(matches) != 1:
            ct = self.content_type
            raise ValueError(
                f"Expected exactly one {ct.app_label}.{ct.model} created resource on previous "
                f"task (task {prev_task.pk}), found {len(matches)}."
            )
        return str(matches[0].object_id)


class WorkflowTaskArg(_WorkflowTaskArgBase):
    """A positional arg of a ``WorkflowTask``."""

    workflow_task = models.ForeignKey(
        WorkflowTask, related_name="task_args", on_delete=models.CASCADE
    )
    arg_index = models.PositiveIntegerField()

    class Meta(_WorkflowTaskArgBase.Meta):
        unique_together = ("workflow_task", "arg_index")
        ordering = ("arg_index",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(content_type__isnull=True) | models.Q(value__isnull=True),
                name="workflowtaskarg_value_ctype_exclusive",
            ),
        ]


class WorkflowTaskKwarg(_WorkflowTaskArgBase):
    """A keyword arg of a ``WorkflowTask``."""

    workflow_task = models.ForeignKey(
        WorkflowTask, related_name="task_kwargs", on_delete=models.CASCADE
    )
    kwarg_key = models.TextField()

    class Meta(_WorkflowTaskArgBase.Meta):
        unique_together = ("workflow_task", "kwarg_key")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(content_type__isnull=True) | models.Q(value__isnull=True),
                name="workflowtaskkwarg_value_ctype_exclusive",
            ),
        ]
