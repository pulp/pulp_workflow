from gettext import gettext as _

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from pulpcore.plugin.constants import TASK_CHOICES, TASK_STATES
from pulpcore.plugin.models import TaskSchedule
from pulpcore.plugin.serializers import (
    DomainUniqueValidator,
    IdentityField,
    ModelSerializer,
    NestedRelatedField,
    RelatedField,
    pulp_labels_validator,
)

from pulp_workflow.app.models import (
    CALLBACK_TYPE_CHOICES,
    CallbackService,
    Workflow,
    WorkflowCallback,
    WorkflowRun,
    WorkflowRunCallback,
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

    class Meta:
        model = WorkflowTask
        fields = (
            "index",
            "task_name",
            "task_args",
            "task_kwargs",
            "reserved_resources",
        )

    def validate_task_kwargs(self, value):
        keys = [kw["kwarg_key"] for kw in value]
        if len(set(keys)) != len(keys):
            raise serializers.ValidationError(_("kwarg_key values must be unique."))
        return value


class CallbackServiceRelatedField(RelatedField):
    """A hyperlinked relation to a ``CallbackService`` by its detail URL or PRN."""

    view_name = "workflow-callback-services-detail"

    # ``queryset`` is set in ``__init__`` rather than as a class attribute so importing this module
    # does not require Django app loading to be far enough along for ``CallbackService.objects`` to
    # resolve.
    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", CallbackService.objects.all())
        super().__init__(**kwargs)


class WorkflowCallbackSerializer(serializers.ModelSerializer):
    """A ``WorkflowCallback`` nested under a ``Workflow``.

    On create, ``callback_service`` and ``callback_type`` are required. Dispatched callback tasks
    are recorded per run and exposed via ``WorkflowRunSerializer.callbacks``.
    """

    callback_service = CallbackServiceRelatedField(
        help_text=_("Href of the CallbackService to invoke."),
    )
    callback_type = serializers.ChoiceField(
        choices=CALLBACK_TYPE_CHOICES,
        help_text=_(
            "The workflow lifecycle event that triggers this callback. The 'finished' "
            "type fires on any non-canceled terminal state (completed, failed). Callbacks "
            "are not currently supported for cancellation."
        ),
    )

    class Meta:
        model = WorkflowCallback
        fields = ("callback_service", "callback_type")


class WorkflowSerializer(ModelSerializer):
    """Serializer for Workflow with nested tasks.

    A Workflow is a *definition* plus a schedule; each execution is tracked by a separate
    ``WorkflowRun`` (see ``WorkflowRunSerializer``).
    """

    pulp_href = IdentityField(view_name="workflow-workflows-detail")
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
    start_time = serializers.DateTimeField(
        help_text=_(
            "When the workflow should first run. Defaults to now (immediate). A pulpcore "
            "TaskSchedule is registered at this time to start a workflow run."
        ),
        required=False,
    )
    dispatch_interval = serializers.DurationField(
        help_text=_(
            "If set, the workflow re-runs on this recurring interval (a new run is created each "
            "time). If null (the default), the workflow runs exactly once at start_time."
        ),
        required=False,
        allow_null=True,
    )
    status = serializers.CharField(
        read_only=True,
        help_text=_(
            "Scheduling status: 'scheduled' (a future run is pending), 'completed' (the schedule "
            "has finished firing; check the workflow's runs for the outcome), or 'canceled' (the "
            "workflow was stopped)."
        ),
    )
    next_dispatch = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text=_("When the workflow will next run, or null if no future run is scheduled."),
    )
    tasks = WorkflowTaskSerializer(
        many=True,
        allow_empty=False,
        help_text=_("The ordered tasks that make up this workflow."),
    )
    callbacks = WorkflowCallbackSerializer(
        many=True,
        required=False,
        help_text=_("User-registered callbacks that fire on this workflow's lifecycle events."),
    )

    class Meta:
        model = Workflow
        fields = ModelSerializer.Meta.fields + (
            "name",
            "pulp_labels",
            "start_time",
            "dispatch_interval",
            "status",
            "next_dispatch",
            "tasks",
            "callbacks",
        )

    def validate_dispatch_interval(self, value):
        if value is not None and value.total_seconds() <= 0:
            raise serializers.ValidationError(_("Must be a positive duration."))
        return value

    def validate_callbacks(self, value):
        seen = set()
        for row in value:
            key = (row["callback_service"].pk, row["callback_type"])
            if key in seen:
                raise serializers.ValidationError(
                    _("Duplicate (callback_service, callback_type): ({s!r}, {t!r}).").format(
                        s=row["callback_service"].name, t=row["callback_type"]
                    )
                )
            seen.add(key)
        return value

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
        callbacks_data = validated_data.pop("callbacks", [])
        workflow = Workflow.objects.create(**validated_data)
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
        WorkflowCallback.objects.bulk_create(
            WorkflowCallback(workflow=workflow, **row) for row in callbacks_data
        )

        # Schedule dispatch of start_workflow_run at start_time. With dispatch_interval=None
        # pulpcore's scheduler fires it once and stops; with an interval set it fires
        # repeatedly, creating a new WorkflowRun each time.
        TaskSchedule.objects.create(
            name=f"pulp_workflow.workflow:{workflow.pk}",
            task_name="pulp_workflow.app.tasks.start_workflow_run",
            task_kwargs={"workflow_pk": str(workflow.pk)},
            next_dispatch=workflow.start_time,
            dispatch_interval=workflow.dispatch_interval,
        )
        return workflow


class WorkflowRunCallbackSerializer(serializers.ModelSerializer):
    """A callback task dispatched for a single ``WorkflowRun``."""

    callback_service = RelatedField(
        source="workflow_callback.callback_service",
        view_name="workflow-callback-services-detail",
        read_only=True,
        help_text=_("Href of the CallbackService that was invoked."),
    )
    callback_type = serializers.CharField(
        source="workflow_callback.callback_type",
        read_only=True,
        help_text=_("The workflow lifecycle event that triggered this callback."),
    )
    dispatched_task = RelatedField(
        view_name="tasks-detail",
        read_only=True,
        allow_null=True,
        help_text=_("Href of the dispatched callback task, if any."),
    )

    class Meta:
        model = WorkflowRunCallback
        fields = ("callback_service", "callback_type", "dispatched_task")


class WorkflowRunSerializer(ModelSerializer):
    """Serializer for a single execution (run) of a Workflow."""

    pulp_href = NestedRelatedField(
        view_name="runs-detail",
        parent_lookup_kwargs={"workflow_pk": "workflow__pk"},
        read_only=True,
        source="*",
    )
    workflow = RelatedField(
        view_name="workflow-workflows-detail",
        read_only=True,
        help_text=_("Href of the workflow this run executes."),
    )
    state = serializers.ChoiceField(
        choices=TASK_CHOICES,
        help_text=_("The current state of the run."),
        read_only=True,
    )
    started_at = serializers.DateTimeField(
        help_text=_("Timestamp of when this run started execution."),
        read_only=True,
        allow_null=True,
    )
    finished_at = serializers.DateTimeField(
        help_text=_("Timestamp of when this run stopped execution."),
        read_only=True,
        allow_null=True,
    )
    error = serializers.JSONField(
        help_text=_(
            "A JSON object describing a fatal error encountered during the execution of this run."
        ),
        read_only=True,
        allow_null=True,
    )
    current_task = RelatedField(
        view_name="tasks-detail",
        read_only=True,
        allow_null=True,
        help_text=_("Href of the pulpcore Task most recently dispatched for this run, if any."),
    )
    task_group = RelatedField(
        view_name="task-groups-detail",
        read_only=True,
        allow_null=True,
        help_text=_(
            "Href of the pulpcore TaskGroup containing tasks dispatched by this run "
            "(child tasks and execute_workflow continuations)."
        ),
    )
    callbacks = WorkflowRunCallbackSerializer(
        many=True,
        read_only=True,
        help_text=_("Callback tasks dispatched for this run."),
    )

    class Meta:
        model = WorkflowRun
        fields = ModelSerializer.Meta.fields + (
            "workflow",
            "state",
            "started_at",
            "finished_at",
            "error",
            "current_task",
            "task_group",
            "callbacks",
        )


class WorkflowCancelSerializer(serializers.Serializer):
    """Serializer used to validate the body of a cancel (PATCH) request.

    Used both to stop a workflow's schedule (and cancel its in-flight runs) and to cancel a
    single ``WorkflowRun``.
    """

    state = serializers.ChoiceField(
        choices=[(TASK_STATES.CANCELED, "Canceled")],
        help_text=_("The desired state of the workflow. Only 'canceled' is accepted."),
        required=True,
    )

    class Meta:
        fields = ("state",)


class CallbackServiceSerializer(ModelSerializer):
    """Serializer for ``CallbackService``."""

    pulp_href = IdentityField(view_name="workflow-callback-services-detail")
    name = serializers.CharField(
        help_text=_("A name for this callback service. Unique within a domain."),
        validators=[DomainUniqueValidator(queryset=CallbackService.objects.all())],
    )
    script = serializers.CharField(
        help_text=_(
            "An absolute path on the Pulp worker host to an executable script that is "
            "invoked when an attached workflow reaches the registered callback type. "
            "Workflow context is exposed via PULP_WORKFLOW_* environment variables; the "
            "exact subset is controlled by the ``WORKFLOW_CALLBACK_FIELDS`` server setting "
            "(defaults to PULP_WORKFLOW_NAME and PULP_WORKFLOW_STATE). PULP_WORKFLOW_PK, "
            "PULP_WORKFLOW_LABELS, and PULP_WORKFLOW_LABEL_<KEY> may also be exposed."
        ),
    )

    def validate_script(self, value):
        # Run the same checks that ``CallbackService.validate`` enforces on save, but raise a DRF
        # ``ValidationError`` so the API returns 400 rather than 500 for misconfigured input.
        try:
            CallbackService(script=value).validate()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        return value

    class Meta:
        model = CallbackService
        fields = ModelSerializer.Meta.fields + ("name", "script")
