import functools

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status
from rest_framework.response import Response

from pulpcore.plugin.constants import TASK_FINAL_STATES, TASK_STATES
from pulpcore.plugin.models import TaskSchedule
from pulpcore.plugin.tasking import cancel_task_group
from pulpcore.plugin.viewsets import (
    DATETIME_FILTER_OPTIONS,
    NAME_FILTER_OPTIONS,
    BaseFilterSet,
    LabelFilter,
    LabelsMixin,
    NamedModelViewSet,
    RolesMixin,
)

from pulp_workflow.app.models import CallbackService, Workflow, WorkflowRun
from pulp_workflow.app.serializers import (
    CallbackServiceSerializer,
    WorkflowCancelSerializer,
    WorkflowRunSerializer,
    WorkflowSerializer,
)


class WorkflowPluginViewSetMixin:
    """Mixin that mounts every ``pulp_workflow`` endpoint under ``/workflow/``.

    Pulpcore does not automatically scope plain (non-Master/Detail) plugin viewsets under their
    plugin name, so ``endpoint_pieces`` is overridden here to prepend ``"workflow"``. This keeps
    ``pulp_workflow``'s endpoints (``/pulp/api/v3/workflow/workflows/``,
    ``/pulp/api/v3/workflow/callback-services/``) grouped under a stable plugin prefix.

    Implemented as a plain mixin rather than a ``NamedModelViewSet`` subclass so that pulpcore's
    ``import_viewsets`` does not try to introspect a ``queryset`` on it during app startup.
    Must be listed *before* ``NamedModelViewSet`` in a viewset's MRO so ``super()`` resolves to
    the right ``endpoint_pieces`` implementation.
    """

    @classmethod
    def endpoint_pieces(cls):
        return ["workflow", *super().endpoint_pieces()]


class WorkflowFilter(BaseFilterSet):
    """Filter for Workflows.

    ``BaseFilterSet`` contributes ``pulp_href``/``pulp_href__in``, ``pulp_id__in``,
    ``prn__in``, and the ``q`` expression filter automatically.
    """

    pulp_label_select = LabelFilter()

    class Meta:
        model = Workflow
        fields = {
            "name": NAME_FILTER_OPTIONS,
            "pulp_created": DATETIME_FILTER_OPTIONS,
            "start_time": DATETIME_FILTER_OPTIONS,
        }


class WorkflowViewSet(
    WorkflowPluginViewSetMixin,
    NamedModelViewSet,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    LabelsMixin,
    RolesMixin,
):
    """
    A ViewSet for managing Workflows.

    A Workflow is a definition plus a schedule; it is immutable after creation except that it
    may be *stopped* (PATCH with ``{"state": "canceled"}``), which removes its schedule and
    cancels any in-flight runs. To change a workflow, stop it and create a new one. Individual
    executions are exposed as ``WorkflowRun`` resources.
    """

    queryset = Workflow.annotate_schedule(Workflow.objects.all()).prefetch_related(
        "tasks",
        "callbacks",
        "callbacks__callback_service",
    )
    endpoint_name = "workflows"
    router_lookup = "workflow"
    pulp_tag_name = "Workflows"
    serializer_class = WorkflowSerializer
    filterset_class = WorkflowFilter
    ordering = "-pulp_created"
    queryset_filtering_required_permission = "workflow.view_workflow"

    DEFAULT_ACCESS_POLICY = {
        "statements": [
            {
                "action": ["list", "retrieve", "my_permissions"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:workflow.view_workflow",
            },
            {
                "action": [
                    "create",
                    "update",
                    "partial_update",
                    "set_label",
                    "unset_label",
                    "list_roles",
                    "add_role",
                    "remove_role",
                ],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:workflow.change_workflow",
            },
        ],
        "queryset_scoping": {"function": "scope_queryset"},
    }
    LOCKED_ROLES = {
        "workflow.workflow_admin": [
            "workflow.view_workflow",
            "workflow.change_workflow",
            "workflow.manage_roles_workflow",
            "workflow.view_workflowrun",
            "workflow.change_workflowrun",
        ],
        "workflow.workflow_viewer": [
            "workflow.view_workflow",
            "workflow.view_workflowrun",
        ],
    }

    def get_serializer_class(self):
        if self.action == "partial_update":
            return WorkflowCancelSerializer
        return super().get_serializer_class()

    @extend_schema(
        description=(
            "Stop a workflow. Removes the workflow's schedule so no further runs are created "
            "and cancels any of its runs that are still in progress. Idempotent."
        ),
        summary="Stop a workflow",
        operation_id="workflows_cancel",
        responses={200: WorkflowSerializer},
    )
    def partial_update(self, request, pk=None, partial=True):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        workflow = self.get_object()

        # Cancel any in-flight runs.
        with transaction.atomic():
            # Lock the workflow to serialize with start_workflow_run, so a run it is
            # concurrently creating can't slip past the cancel query below.
            workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)
            # Stop future runs by removing the schedule inside the same transaction, so
            # the whole stop operation (schedule removal + run cancellation) commits
            # atomically. Done under the workflow lock, this also serializes with
            # start_workflow_run, which checks the schedule's existence under the same lock.
            TaskSchedule.objects.filter(name=f"pulp_workflow.workflow:{workflow.pk}").delete()
            runs = (
                WorkflowRun.objects.select_for_update()
                .filter(workflow=workflow)
                .exclude(state__in=TASK_FINAL_STATES)
            )
            for run in runs:
                run.state = TASK_STATES.CANCELED
                run.finished_at = timezone.now()
                run.save(update_fields=["state", "finished_at", "pulp_last_updated"])
                # Cancel in-flight child tasks and queued continuations only after the
                # outermost transaction commits, so workers don't observe stale state.
                if run.task_group_id is not None:
                    transaction.on_commit(functools.partial(cancel_task_group, run.task_group_id))

        out = WorkflowSerializer(workflow, context={"request": request})
        return Response(out.data)


class CallbackServiceFilter(BaseFilterSet):
    """Filter for CallbackServices."""

    class Meta:
        model = CallbackService
        fields = {
            "name": NAME_FILTER_OPTIONS,
            "pulp_created": DATETIME_FILTER_OPTIONS,
        }


class CallbackServiceViewSet(
    WorkflowPluginViewSetMixin,
    NamedModelViewSet,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    RolesMixin,
):
    """A ViewSet for managing CallbackServices.

    A CallbackService points at an absolute path to an executable on the Pulp worker host. It is
    invoked when an attached Workflow reaches a registered lifecycle event. Because callbacks run
    arbitrary host scripts they are treated as a privileged resource and require
    ``callbackservice_admin``-level permissions to manage.
    """

    queryset = CallbackService.objects.all()
    endpoint_name = "callback-services"
    pulp_tag_name = "Callback Services"
    serializer_class = CallbackServiceSerializer
    filterset_class = CallbackServiceFilter
    ordering = "-pulp_created"
    queryset_filtering_required_permission = "workflow.view_callbackservice"

    DEFAULT_ACCESS_POLICY = {
        "statements": [
            {
                "action": ["list", "retrieve", "my_permissions"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:workflow.view_callbackservice",
            },
            {
                "action": ["create"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_perms:workflow.add_callbackservice",
            },
            {
                "action": ["update", "partial_update"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:workflow.change_callbackservice",
            },
            {
                "action": ["destroy"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:workflow.delete_callbackservice",
            },
            {
                "action": ["list_roles", "add_role", "remove_role"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": (
                    "has_model_or_domain_or_obj_perms:workflow.manage_roles_callbackservice"
                ),
            },
        ],
        "queryset_scoping": {"function": "scope_queryset"},
    }
    LOCKED_ROLES = {
        "workflow.callbackservice_admin": [
            "workflow.view_callbackservice",
            "workflow.add_callbackservice",
            "workflow.change_callbackservice",
            "workflow.delete_callbackservice",
            "workflow.manage_roles_callbackservice",
        ],
        "workflow.callbackservice_viewer": ["workflow.view_callbackservice"],
    }


class WorkflowRunFilter(BaseFilterSet):
    """Filter for WorkflowRuns.

    Shared by the nested (``/workflow/workflows/<workflow_pk>/runs/``) and flat
    (``/workflow/workflow-runs/``) run endpoints. The ``workflow`` filter is primarily useful on
    the flat endpoint to scope to a single workflow's run history (or, via ``workflow__in``, to
    batch-fetch the runs for several workflows in one request); on the nested endpoint the
    workflow is already implied by the URL.
    """

    class Meta:
        model = WorkflowRun
        fields = {
            "workflow": ["exact", "in"],
            "state": ["exact", "in", "ne"],
            "pulp_created": DATETIME_FILTER_OPTIONS,
            "started_at": DATETIME_FILTER_OPTIONS,
            "finished_at": DATETIME_FILTER_OPTIONS,
        }


class WorkflowRunViewSet(
    NamedModelViewSet,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
):
    """A ViewSet for inspecting and canceling individual Workflow runs.

    Runs are nested under their workflow at ``/workflow/workflows/<workflow_pk>/runs/``. They are
    created by the scheduler (never directly through the API), so this ViewSet exposes only read
    actions plus a cancel (PATCH).
    """

    queryset = (
        WorkflowRun.objects.all()
        .select_related("workflow", "current_task", "task_group")
        .prefetch_related(
            "callbacks__dispatched_task",
            "callbacks__workflow_callback__callback_service",
        )
    )
    endpoint_name = "runs"
    nest_prefix = "workflow/workflows"
    router_lookup = "run"
    parent_viewset = WorkflowViewSet
    parent_lookup_kwargs = {"workflow_pk": "workflow__pk"}
    pulp_tag_name = "Workflow Runs"
    serializer_class = WorkflowRunSerializer
    filterset_class = WorkflowRunFilter
    ordering = "-pulp_created"
    queryset_filtering_required_permission = "workflow.view_workflowrun"

    DEFAULT_ACCESS_POLICY = {
        "statements": [
            {
                "action": ["list", "retrieve", "my_permissions"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:workflow.view_workflowrun",
            },
            {
                "action": ["partial_update"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:workflow.change_workflowrun",
            },
        ],
        "queryset_scoping": {"function": "scope_queryset"},
    }

    def get_serializer_class(self):
        if self.action == "partial_update":
            return WorkflowCancelSerializer
        return super().get_serializer_class()

    @extend_schema(
        description=(
            "Cancel a workflow run. A run can be canceled while waiting or running; canceling a "
            "run that has already reached a terminal state returns 409."
        ),
        summary="Cancel a workflow run",
        operation_id="workflow_runs_cancel",
        responses={200: WorkflowRunSerializer, 409: WorkflowRunSerializer},
    )
    def partial_update(self, request, workflow_pk=None, pk=None, partial=True):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = self.get_object()
        with transaction.atomic():
            run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
            if run.state in TASK_FINAL_STATES:
                http_status = status.HTTP_409_CONFLICT
            else:
                run.state = TASK_STATES.CANCELED
                run.finished_at = timezone.now()
                run.save(update_fields=["state", "finished_at", "pulp_last_updated"])
                if run.task_group_id is not None:
                    transaction.on_commit(functools.partial(cancel_task_group, run.task_group_id))
                http_status = None

        out = WorkflowRunSerializer(run, context={"request": request})
        return Response(out.data, status=http_status)


class WorkflowRunListViewSet(
    WorkflowPluginViewSetMixin,
    NamedModelViewSet,
    mixins.ListModelMixin,
):
    """A flat, list-only view of every WorkflowRun across all workflows.

    Complements the nested ``/workflow/workflows/<workflow_pk>/runs/`` endpoint by exposing all
    runs at a single top-level collection (``/workflow/workflow-runs/``). Filter by ``workflow`` to
    scope to a single workflow's run history. Individual runs are retrieved and canceled through
    their canonical nested URL (served by ``WorkflowRunViewSet``); each run's ``pulp_href`` points
    there regardless of which endpoint listed it.
    """

    queryset = (
        WorkflowRun.objects.all()
        .select_related("workflow", "current_task", "task_group")
        .prefetch_related(
            "callbacks__dispatched_task",
            "callbacks__workflow_callback__callback_service",
        )
    )
    endpoint_name = "workflow-runs"
    pulp_tag_name = "Workflow Run List"
    serializer_class = WorkflowRunSerializer
    filterset_class = WorkflowRunFilter
    ordering = "-pulp_created"
    queryset_filtering_required_permission = "workflow.view_workflowrun"

    DEFAULT_ACCESS_POLICY = {
        "statements": [
            {
                "action": ["list"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:workflow.view_workflowrun",
            },
        ],
        "queryset_scoping": {"function": "scope_queryset"},
    }
