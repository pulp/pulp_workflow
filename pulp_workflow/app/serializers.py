from gettext import gettext as _

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from pulpcore.plugin.constants import TASK_CHOICES, TASK_STATES
from pulpcore.plugin.models import TaskGroup, TaskSchedule
from pulpcore.plugin.serializers import (
    IdentityField,
    ModelSerializer,
    RelatedField,
    pulp_labels_validator,
)

from pulp_workflow.app.models import (
    Workflow,
    WorkflowTask,
    WorkflowTaskArg,
    WorkflowTaskKwarg,
)


class ContentTypeNaturalKeyField(serializers.CharField):
    """A ``ContentType`` field serialized as ``"app_label.model"``."""

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value.count(".") != 1:
            raise serializers.ValidationError(
                _("Must be 'app_label.model', got {v!r}.").format(v=value)
            )
        try:
            return ContentType.objects.get_by_natural_key(*value.split("."))
        except ContentType.DoesNotExist:
            raise serializers.ValidationError(_("Unknown content type {v!r}.").format(v=value))

    def to_representation(self, value):
        return f"{value.app_label}.{value.model}"


class WorkflowTaskArgSerializer(serializers.ModelSerializer):
    """A single positional arg of a ``WorkflowTask``."""

    arg_index = serializers.IntegerField(
        read_only=True,
        help_text=_("Position of this arg, assigned from the order of the ``task_args`` list."),
    )
    value = serializers.JSONField(
        required=False,
        allow_null=True,
        write_only=True,
        help_text=_("Literal value passed to the task. Write-only; values may be sensitive."),
    )
    content_type = ContentTypeNaturalKeyField(
        required=False,
        allow_null=True,
        help_text=_(
            "If set, the 'app_label.model' of the previous task's created resource to resolve "
            "to a primary key at dispatch time. Mutually exclusive with ``value``."
        ),
    )

    class Meta:
        model = WorkflowTaskArg
        fields = ("arg_index", "value", "content_type")

    def validate(self, data):
        if data.get("content_type") is not None and data.get("value") is not None:
            raise serializers.ValidationError(
                _("`content_type` and `value` are mutually exclusive.")
            )
        return data


class WorkflowTaskKwargSerializer(serializers.ModelSerializer):
    """A single keyword arg of a ``WorkflowTask``."""

    kwarg_key = serializers.CharField()
    value = serializers.JSONField(
        required=False,
        allow_null=True,
        write_only=True,
        help_text=_("Literal value passed to the task. Write-only; values may be sensitive."),
    )
    content_type = ContentTypeNaturalKeyField(
        required=False,
        allow_null=True,
        help_text=_(
            "If set, the 'app_label.model' of the previous task's created resource to resolve "
            "to a primary key at dispatch time. Mutually exclusive with ``value``."
        ),
    )

    class Meta:
        model = WorkflowTaskKwarg
        fields = ("kwarg_key", "value", "content_type")

    def validate(self, data):
        if data.get("content_type") is not None and data.get("value") is not None:
            raise serializers.ValidationError(
                _("`content_type` and `value` are mutually exclusive.")
            )
        return data


class WorkflowTaskSerializer(serializers.ModelSerializer):
    """Serializer for a single task within a Workflow.

    Tasks are nested resources of a Workflow and have no standalone endpoint, so
    this uses DRF's plain ``ModelSerializer`` rather than pulpcore's hyperlinked
    base.
    """

    index = serializers.IntegerField(
        read_only=True,
        help_text=_(
            "Execution order of this task within the workflow, assigned from the order of "
            "the ``tasks`` list."
        ),
    )
    task_name = serializers.CharField(
        help_text=_("The name of the task to be dispatched."),
    )
    task_args = WorkflowTaskArgSerializer(
        many=True,
        required=False,
        help_text=_("Positional arguments passed to the task."),
    )
    task_kwargs = WorkflowTaskKwargSerializer(
        many=True,
        required=False,
        help_text=_("Keyword arguments passed to the task."),
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

    def validate_task_kwargs(self, value):
        keys = [kw["kwarg_key"] for kw in value]
        if len(set(keys)) != len(keys):
            raise serializers.ValidationError(_("kwarg_key values must be unique."))
        return value


class WorkflowSerializer(ModelSerializer):
    """Serializer for Workflow with nested tasks."""

    pulp_href = IdentityField(view_name="workflows-detail")
    name = serializers.CharField(
        help_text=_("The name of the workflow."),
        allow_blank=False,
        validators=[UniqueValidator(queryset=Workflow.objects.all())],
    )
    pulp_labels = serializers.HStoreField(
        help_text=_("A dictionary of arbitrary labels to associate with the workflow."),
        required=False,
        validators=[pulp_labels_validator],
    )
    state = serializers.ChoiceField(
        choices=TASK_CHOICES,
        help_text=_("The current state of the workflow."),
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
    current_task = RelatedField(
        view_name="tasks-detail",
        read_only=True,
        source="current_task.dispatched_task",
        allow_null=True,
        help_text=_(
            "Href of the pulpcore Task currently being dispatched for this workflow, if any."
        ),
    )
    task_group = RelatedField(
        view_name="task-groups-detail",
        read_only=True,
        allow_null=True,
        help_text=_(
            "Href of the pulpcore TaskGroup containing tasks dispatched by this workflow "
            "(child tasks and execute_workflow continuations)."
        ),
    )
    tasks = WorkflowTaskSerializer(
        many=True,
        allow_empty=False,
        help_text=_("The ordered tasks that make up this workflow."),
    )

    class Meta:
        model = Workflow
        fields = ModelSerializer.Meta.fields + (
            "name",
            "pulp_labels",
            "state",
            "start_time",
            "started_at",
            "finished_at",
            "error",
            "current_task",
            "task_group",
            "tasks",
        )

    def validate_tasks(self, value):
        # Dynamic args reference the previous task's created resources, so the first task
        # cannot use them.
        first = value[0]
        rows = first.get("task_args", []) + first.get("task_kwargs", [])
        if any(row.get("content_type") is not None for row in rows):
            raise serializers.ValidationError(
                _("The first task cannot have dynamic args (no previous task).")
            )
        return value

    @transaction.atomic
    def create(self, validated_data):
        tasks_data = validated_data.pop("tasks")
        workflow = Workflow.objects.create(**validated_data)
        workflow.task_group = TaskGroup.objects.create(
            description=f"Workflow: {workflow.name}",
            pulp_domain=workflow.pulp_domain,
        )
        workflow.save(update_fields=["task_group", "pulp_last_updated"])
        for task_index, task_data in enumerate(tasks_data):
            task_args = task_data.pop("task_args", [])
            task_kwargs = task_data.pop("task_kwargs", [])
            wf_task = WorkflowTask.objects.create(workflow=workflow, index=task_index, **task_data)
            WorkflowTaskArg.objects.bulk_create(
                WorkflowTaskArg(workflow_task=wf_task, arg_index=arg_index, **row)
                for arg_index, row in enumerate(task_args)
            )
            WorkflowTaskKwarg.objects.bulk_create(
                WorkflowTaskKwarg(workflow_task=wf_task, **row) for row in task_kwargs
            )

        # Schedule a one-shot dispatch of execute_workflow at start_time.
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

    state = serializers.ChoiceField(
        choices=[(TASK_STATES.CANCELED, "Canceled")],
        help_text=_("The desired state of the workflow. Only 'canceled' is accepted."),
        required=True,
    )

    class Meta:
        fields = ("state",)
