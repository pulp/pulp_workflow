import asyncio
import json
import os
import re
import subprocess
from gettext import gettext as _

from django.conf import settings
from django.contrib.postgres.fields import ArrayField, HStoreField
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models
from django.utils import timezone
from django_guid import get_guid

from pulpcore.plugin.constants import TASK_CHOICES, TASK_STATES
from pulpcore.plugin.models import BaseModel, EncryptedJSONField
from pulpcore.plugin.util import get_domain_pk


# ---------------------------------------------------------------------------
# Callback type constants. These are the events on which a CallbackService can be triggered.
# The first set mirror Workflow lifecycle states; ``FINISHED`` is a synthetic type that fires on
# any terminal state (completed, failed, canceled).
# ---------------------------------------------------------------------------
class CALLBACK_TYPES:  # noqa: N801 - mirror pulpcore.constants style (TASK_STATES, ...)
    RUNNING = TASK_STATES.RUNNING
    COMPLETED = TASK_STATES.COMPLETED
    FAILED = TASK_STATES.FAILED
    CANCELED = TASK_STATES.CANCELED
    # Wildcard: fires on any terminal state.
    FINISHED = "finished"


CALLBACK_TYPE_CHOICES = (
    (CALLBACK_TYPES.RUNNING, "Running"),
    (CALLBACK_TYPES.COMPLETED, "Completed"),
    (CALLBACK_TYPES.FAILED, "Failed"),
    (CALLBACK_TYPES.CANCELED, "Canceled"),
    (CALLBACK_TYPES.FINISHED, "Finished"),
)

# Map a workflow state transition to the set of callback types that should fire. The key is the
# workflow's new state; the value is the tuple of CallbackService callback_type values to dispatch.
TRANSITION_CALLBACK_TYPES = {
    TASK_STATES.RUNNING: (CALLBACK_TYPES.RUNNING,),
    TASK_STATES.COMPLETED: (CALLBACK_TYPES.COMPLETED, CALLBACK_TYPES.FINISHED),
    TASK_STATES.FAILED: (CALLBACK_TYPES.FAILED, CALLBACK_TYPES.FINISHED),
    TASK_STATES.CANCELED: (CALLBACK_TYPES.CANCELED, CALLBACK_TYPES.FINISHED),
}

# Env var keys must be POSIX-portable: [A-Z_][A-Z0-9_]*. Sanitize label keys to fit that shape
# before exposing them as PULP_WORKFLOW_LABEL_<KEY>.
_ENV_KEY_RE = re.compile(r"[^A-Z0-9_]")

# Valid scalar entries for ``WORKFLOW_CALLBACK_FIELDS``. ``labels:<key>`` is also accepted to
# expose a single label without leaking the rest.
ALLOWED_CALLBACK_FIELDS = frozenset({"pk", "name", "state", "labels"})


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


class CallbackService(BaseModel):
    """
    A user-registered subprocess invoked when a Workflow reaches a lifecycle event.

    Modeled after pulpcore's ``SigningService``: the ``script`` field is an absolute path to an
    executable on the Pulp worker host. At registration time the script is validated for existence
    and the executable bit; when invoked it is run as a subprocess with workflow context exposed
    via environment variables (``PULP_WORKFLOW_NAME``, ``PULP_WORKFLOW_STATE``, etc.). Which
    workflow fields are exposed is controlled by the ``WORKFLOW_CALLBACK_FIELDS`` setting.

    Unlike ``SigningService`` (which is admin-installed out of band), ``CallbackService`` is fully
    API-managed and RBAC-scoped.
    """

    name = models.TextField()
    script = models.TextField()

    pulp_domain = models.ForeignKey("core.Domain", default=get_domain_pk, on_delete=models.CASCADE)

    def __str__(self):
        return f"CallbackService: {self.name}"

    def _env(self, workflow, env_vars=None):
        """Build the env dict passed to the script. Honors ``WORKFLOW_CALLBACK_FIELDS``."""
        guid = get_guid()
        env = {"CORRELATION_ID": guid if guid else ""}

        fields = getattr(settings, "WORKFLOW_CALLBACK_FIELDS", ["name", "state"]) or []
        scalar_fields = set()
        label_keys = set()
        expose_all_labels = False
        unknown = []
        for entry in fields:
            if entry.startswith("labels:"):
                key = entry[len("labels:") :]
                if key:
                    label_keys.add(key)
                else:
                    unknown.append(entry)
            elif entry == "labels":
                expose_all_labels = True
            elif entry in ALLOWED_CALLBACK_FIELDS:
                scalar_fields.add(entry)
            else:
                unknown.append(entry)
        if unknown:
            raise ImproperlyConfigured(
                _("WORKFLOW_CALLBACK_FIELDS contains unknown entries: {unknown!r}.").format(
                    unknown=sorted(unknown)
                )
            )

        if "pk" in scalar_fields:
            env["PULP_WORKFLOW_PK"] = str(workflow.pk)
        if "name" in scalar_fields:
            env["PULP_WORKFLOW_NAME"] = workflow.name
        if "state" in scalar_fields:
            env["PULP_WORKFLOW_STATE"] = workflow.state
        if expose_all_labels or label_keys:
            labels = workflow.pulp_labels or {}
            if expose_all_labels:
                env["PULP_WORKFLOW_LABELS"] = json.dumps(labels, sort_keys=True)
                items = labels.items()
            else:
                items = ((key, labels.get(key)) for key in label_keys)
            for key, value in items:
                safe_key = _ENV_KEY_RE.sub("_", key.upper())
                env[f"PULP_WORKFLOW_LABEL_{safe_key}"] = "" if value is None else str(value)

        if env_vars:
            env.update(env_vars)
        # Inherit the worker's environment so PATH, etc. work in the script.
        return {**os.environ, **env}

    def run(self, workflow, env_vars=None):
        """Run the script synchronously with workflow context.

        Returns a dict with ``returncode``, ``stdout`` and ``stderr``. Raises ``RuntimeError`` on a
        non-zero exit so the surrounding task records the failure.
        """
        completed = subprocess.run(
            [self.script],
            env=self._env(workflow, env_vars=env_vars),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        result = {
            "returncode": completed.returncode,
            "stdout": completed.stdout.decode("utf-8", errors="replace"),
            "stderr": completed.stderr.decode("utf-8", errors="replace"),
        }
        if completed.returncode != 0:
            raise RuntimeError(
                _("CallbackService {name!r} exited with {rc}: {err}").format(
                    name=self.name, rc=completed.returncode, err=result["stderr"]
                )
            )
        return result

    async def arun(self, workflow, env_vars=None):
        """Async equivalent of :meth:`run`."""
        process = await asyncio.create_subprocess_exec(
            self.script,
            env=self._env(workflow, env_vars=env_vars),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        result = {
            "returncode": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
        if process.returncode != 0:
            raise RuntimeError(
                _("CallbackService {name!r} exited with {rc}: {err}").format(
                    name=self.name, rc=process.returncode, err=result["stderr"]
                )
            )
        return result

    def validate(self):
        """
        Validate that the script is an absolute path to an executable file.

        Raises ``django.core.exceptions.ValidationError`` if the script cannot be invoked. Called
        from :meth:`save` so misconfigured services are rejected before they're persisted, and
        from the serializer's ``validate_script`` so API clients get a 400 rather than a 500.
        """
        if not self.script:
            raise ValidationError(_("`script` is required."))
        if not os.path.isabs(self.script):
            raise ValidationError(
                _("`script` must be an absolute path, got {p!r}.").format(p=self.script)
            )
        if not os.path.isfile(self.script):
            raise ValidationError(
                _("`script` does not exist or is not a file: {p!r}.").format(p=self.script)
            )
        if not os.access(self.script, os.X_OK):
            raise ValidationError(_("`script` is not executable: {p!r}.").format(p=self.script))

    def save(self, *args, **kwargs):
        self.validate()
        super().save(*args, **kwargs)

    class Meta:
        default_permissions = ("add", "change", "delete", "view")
        permissions = [
            ("manage_roles_callbackservice", "Can manage role assignments on callback services"),
        ]
        unique_together = ("pulp_domain", "name")


class WorkflowCallback(BaseModel):
    """
    Attaches a ``CallbackService`` to a ``Workflow`` for a specific lifecycle event.

    A workflow may have any number of callbacks, but each
    ``(workflow, callback_service, callback_type)`` triple is unique. When the workflow reaches the
    event named by ``callback_type``, a Pulp task is dispatched that runs the service; the
    dispatched task is recorded on ``dispatched_task`` so callers can inspect its result via the
    API.
    """

    workflow = models.ForeignKey(Workflow, related_name="callbacks", on_delete=models.CASCADE)
    callback_service = models.ForeignKey(
        CallbackService, related_name="workflow_callbacks", on_delete=models.PROTECT
    )
    callback_type = models.TextField(choices=CALLBACK_TYPE_CHOICES)
    dispatched_task = models.ForeignKey(
        "core.Task",
        null=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )

    def __str__(self):
        return (
            f"WorkflowCallback: {self.workflow.name} -> "
            f"{self.callback_service.name} on {self.callback_type}"
        )

    class Meta:
        unique_together = ("workflow", "callback_service", "callback_type")
