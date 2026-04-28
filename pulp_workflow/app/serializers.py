from gettext import gettext as _

from django.db import transaction
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from pulpcore.plugin.models import TaskSchedule
from pulpcore.plugin.serializers import IdentityField, ModelSerializer, RelatedField

from pulp_workflow.app.models import TaskPlan, TaskPlanStep
from pulp_workflow.app.tasks import PREV_RESOURCE_MARKER


def _validate_markers(value, step_index, field):
    """Recursively check ``$prev_resource`` markers nested in ``value``.

    Markers must be a one-key dict whose value is an ``app_label.model`` string,
    and they cannot appear in step 0 (no previous step exists).
    """
    if isinstance(value, dict) and PREV_RESOURCE_MARKER in value:
        if step_index == 0:
            raise serializers.ValidationError(
                _("Step 0 cannot use {marker!r} (no previous step).").format(
                    marker=PREV_RESOURCE_MARKER
                )
            )
        if len(value) != 1:
            raise serializers.ValidationError(
                _(
                    "Step {idx} {field}: a {marker!r} marker must be the only key in its dict."
                ).format(idx=step_index, field=field, marker=PREV_RESOURCE_MARKER)
            )
        model_key = value[PREV_RESOURCE_MARKER]
        if not isinstance(model_key, str) or model_key.count(".") != 1:
            raise serializers.ValidationError(
                _(
                    "Step {idx} {field}: {marker!r} value must be 'app_label.model', got {v!r}."
                ).format(idx=step_index, field=field, marker=PREV_RESOURCE_MARKER, v=model_key)
            )
        return
    if isinstance(value, list):
        for item in value:
            _validate_markers(item, step_index, field)
    elif isinstance(value, dict):
        for item in value.values():
            _validate_markers(item, step_index, field)


class TaskPlanStepSerializer(serializers.ModelSerializer):
    """Serializer for a single step within a TaskPlan.

    Steps are nested resources of a TaskPlan and have no standalone endpoint, so
    this uses DRF's plain ``ModelSerializer`` rather than pulpcore's hyperlinked
    base.
    """

    index = serializers.IntegerField(
        help_text=_("Execution order of this step within the plan."),
        min_value=0,
    )
    task_name = serializers.CharField(
        help_text=_("The name of the task to be dispatched for this step."),
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
        help_text=_("Resources to reserve when dispatching this step."),
        required=False,
        allow_null=True,
    )
    dispatched_task = RelatedField(
        help_text=_("The task dispatched for this step, if any."),
        read_only=True,
        view_name="tasks-detail",
    )

    class Meta:
        model = TaskPlanStep
        fields = (
            "index",
            "task_name",
            "task_args",
            "task_kwargs",
            "reserved_resources",
            "dispatched_task",
        )


class TaskPlanSerializer(ModelSerializer):
    """Serializer for TaskPlan with nested steps."""

    pulp_href = IdentityField(view_name="workflow-task-plans-detail")
    name = serializers.CharField(
        help_text=_("The name of the task plan."),
        allow_blank=False,
        validators=[UniqueValidator(queryset=TaskPlan.objects.all())],
    )
    state = serializers.CharField(
        help_text=_(
            "The current state of the plan. The possible values include:"
            " 'waiting', 'skipped', 'running', 'completed', 'failed', 'canceled' and 'canceling'."
        ),
        read_only=True,
    )
    start_time = serializers.DateTimeField(
        help_text=_(
            "When the plan should begin executing. Defaults to now (immediate). A pulpcore "
            "TaskSchedule is created at this time to dispatch the execute_task_plan task."
        ),
        required=False,
    )
    started_at = serializers.DateTimeField(
        help_text=_("Timestamp of when this plan started execution."),
        read_only=True,
    )
    finished_at = serializers.DateTimeField(
        help_text=_("Timestamp of when this plan stopped execution."),
        read_only=True,
    )
    error = serializers.JSONField(
        help_text=_(
            "A JSON object describing a fatal error encountered during the execution of this plan."
        ),
        read_only=True,
    )
    current_step = serializers.SerializerMethodField(
        help_text=_("The index of the step currently being executed, if any."),
    )
    steps = TaskPlanStepSerializer(
        many=True,
        help_text=_("The ordered steps that make up this plan."),
    )

    class Meta:
        model = TaskPlan
        fields = ModelSerializer.Meta.fields + (
            "name",
            "state",
            "start_time",
            "started_at",
            "finished_at",
            "error",
            "current_step",
            "steps",
        )

    def get_current_step(self, obj) -> int | None:
        return obj.current_step.index if obj.current_step_id else None

    def validate_steps(self, value):
        if not value:
            raise serializers.ValidationError(_("A task plan must have at least one step."))
        indexes = [step["index"] for step in value]
        if len(set(indexes)) != len(indexes):
            raise serializers.ValidationError(_("Step indexes must be unique within a plan."))
        for step in value:
            for field in ("task_args", "task_kwargs"):
                _validate_markers(step.get(field), step["index"], field)
        return value

    @transaction.atomic
    def create(self, validated_data):
        steps_data = validated_data.pop("steps")
        plan = TaskPlan.objects.create(**validated_data)
        for step_data in steps_data:
            TaskPlanStep.objects.create(plan=plan, **step_data)

        # Schedule a one-shot dispatch of execute_task_plan at the plan's start_time.
        # dispatch_interval=None makes pulpcore's scheduler fire it once and stop.
        TaskSchedule.objects.create(
            name=f"pulp_workflow.task_plan:{plan.pk}",
            task_name="pulp_workflow.app.tasks.execute_task_plan",
            task_kwargs={"task_plan_pk": str(plan.pk)},
            next_dispatch=plan.start_time,
            dispatch_interval=None,
        )
        return plan
