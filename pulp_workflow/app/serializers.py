from gettext import gettext as _

from django.db import transaction
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from pulpcore.plugin.models import TaskSchedule
from pulpcore.plugin.serializers import IdentityField, ModelSerializer, RelatedField

from pulp_workflow.app.models import Workflow, WorkflowTask
from pulp_workflow.app.tasks import PREV_RESOURCE_MARKER


def _validate_markers(value, task_index, field):
    """Recursively check ``$prev_resource`` markers nested in ``value``.

    Markers must be a one-key dict whose value is an ``app_label.model`` string,
    and they cannot appear in task 0 (no previous task exists).
    """
    if isinstance(value, dict) and PREV_RESOURCE_MARKER in value:
        if task_index == 0:
            raise serializers.ValidationError(
                _("Task 0 cannot use {marker!r} (no previous task).").format(
                    marker=PREV_RESOURCE_MARKER
                )
            )
        if len(value) != 1:
            raise serializers.ValidationError(
                _(
                    "Task {idx} {field}: a {marker!r} marker must be the only key in its dict."
                ).format(idx=task_index, field=field, marker=PREV_RESOURCE_MARKER)
            )
        model_key = value[PREV_RESOURCE_MARKER]
        if not isinstance(model_key, str) or model_key.count(".") != 1:
            raise serializers.ValidationError(
                _(
                    "Task {idx} {field}: {marker!r} value must be 'app_label.model', got {v!r}."
                ).format(idx=task_index, field=field, marker=PREV_RESOURCE_MARKER, v=model_key)
            )
        return
    if isinstance(value, list):
        for item in value:
            _validate_markers(item, task_index, field)
    elif isinstance(value, dict):
        for item in value.values():
            _validate_markers(item, task_index, field)


class WorkflowTaskSerializer(serializers.ModelSerializer):
    """Serializer for a single task within a Workflow.

    Tasks are nested resources of a Workflow and have no standalone endpoint, so
    this uses DRF's plain ``ModelSerializer`` rather than pulpcore's hyperlinked
    base.
    """

    index = serializers.IntegerField(
        help_text=_("Execution order of this task within the workflow."),
        min_value=0,
    )
    task_name = serializers.CharField(
        help_text=_("The name of the task to be dispatched."),
    )
    task_args = serializers.JSONField(
        help_text=_(
            "Positional arguments passed to the task. Write-only; not exposed in responses "
            "because the values may be sensitive."
        ),
        required=False,
        write_only=True,
    )
    task_kwargs = serializers.JSONField(
        help_text=_(
            "Keyword arguments passed to the task. Write-only; not exposed in responses "
            "because the values may be sensitive."
        ),
        required=False,
        write_only=True,
    )
    reserved_resources = serializers.ListField(
        child=serializers.CharField(),
        help_text=_("Resources to reserve when dispatching this task."),
        required=False,
        allow_null=True,
    )
    dispatched_task = RelatedField(
        help_text=_("The task dispatched, if any."),
        read_only=True,
        view_name="tasks-detail",
    )

    class Meta:
        model = WorkflowTask
        fields = (
            "index",
            "task_name",
            "task_args",
            "task_kwargs",
            "reserved_resources",
            "dispatched_task",
        )


class WorkflowSerializer(ModelSerializer):
    """Serializer for Workflow with nested tasks."""

    pulp_href = IdentityField(view_name="workflows-detail")
    name = serializers.CharField(
        help_text=_("The name of the workflow."),
        allow_blank=False,
        validators=[UniqueValidator(queryset=Workflow.objects.all())],
    )
    state = serializers.CharField(
        help_text=_(
            "The current state of the workflow. The possible values include:"
            " 'waiting', 'skipped', 'running', 'completed', 'failed', 'canceled' and 'canceling'."
        ),
        read_only=True,
    )
    start_time = serializers.DateTimeField(
        help_text=_(
            "When the workflow should begin executing. Defaults to now (immediate). A pulpcore "
            "TaskSchedule is created at this time to dispatch the execute_workflow task."
        ),
        required=False,
    )
    started_at = serializers.DateTimeField(
        help_text=_("Timestamp of when this workflow started execution."),
        read_only=True,
    )
    finished_at = serializers.DateTimeField(
        help_text=_("Timestamp of when this workflow stopped execution."),
        read_only=True,
    )
    error = serializers.JSONField(
        help_text=_(
            "A JSON object describing a fatal error encountered during the execution of this "
            "workflow."
        ),
        read_only=True,
    )
    current_task = serializers.SerializerMethodField(
        help_text=_("The index of the task currently being executed, if any."),
    )
    tasks = WorkflowTaskSerializer(
        many=True,
        help_text=_("The ordered tasks that make up this workflow."),
    )

    class Meta:
        model = Workflow
        fields = ModelSerializer.Meta.fields + (
            "name",
            "state",
            "start_time",
            "started_at",
            "finished_at",
            "error",
            "current_task",
            "tasks",
        )

    def get_current_task(self, obj) -> int | None:
        return obj.current_task.index if obj.current_task_id else None

    def validate_tasks(self, value):
        if not value:
            raise serializers.ValidationError(_("A workflow must have at least one task."))
        indexes = [task["index"] for task in value]
        if len(set(indexes)) != len(indexes):
            raise serializers.ValidationError(_("Task indexes must be unique within a workflow."))
        for task in value:
            for field in ("task_args", "task_kwargs"):
                _validate_markers(task.get(field), task["index"], field)
        return value

    @transaction.atomic
    def create(self, validated_data):
        tasks_data = validated_data.pop("tasks")
        workflow = Workflow.objects.create(**validated_data)
        for task_data in tasks_data:
            WorkflowTask.objects.create(workflow=workflow, **task_data)

        # Schedule a one-shot dispatch of execute_workflow at the workflow's start_time.
        # dispatch_interval=None makes pulpcore's scheduler fire it once and stop.
        TaskSchedule.objects.create(
            name=f"pulp_workflow.workflow:{workflow.pk}",
            task_name="pulp_workflow.app.tasks.execute_workflow",
            task_kwargs={"workflow_pk": str(workflow.pk)},
            next_dispatch=workflow.start_time,
            dispatch_interval=None,
        )
        return workflow


class WorkflowCancelSerializer(serializers.Serializer):
    """Serializer used to validate the body of a workflow cancel (PATCH) request."""

    state = serializers.CharField(
        help_text=_("The desired state of the workflow. Only 'canceled' is accepted."),
        required=True,
    )

    def validate_state(self, value):
        if value != "canceled":
            raise serializers.ValidationError(
                _("The only acceptable value for 'state' is 'canceled'.")
            )
        return value

    class Meta:
        fields = ("state",)
