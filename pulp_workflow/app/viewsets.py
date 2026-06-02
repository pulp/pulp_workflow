import functools

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status
from rest_framework.response import Response

from pulpcore.plugin.constants import TASK_STATES
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

from pulp_workflow.app.models import CallbackService, Workflow
from pulp_workflow.app.serializers import (
    CallbackServiceSerializer,
    WorkflowCancelSerializer,
    WorkflowSerializer,
)
from pulp_workflow.app.tasks import dispatch_workflow_callbacks


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
            "state": ["exact", "in", "ne"],
            "pulp_created": DATETIME_FILTER_OPTIONS,
            "start_time": DATETIME_FILTER_OPTIONS,
            "started_at": DATETIME_FILTER_OPTIONS,
            "finished_at": DATETIME_FILTER_OPTIONS,
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

    Workflows are created with their full set of tasks and are immutable thereafter; to
    change a workflow, cancel it (if it has not yet started) and create a new one.
    """

    queryset = (
        Workflow.objects.all()
        .select_related("task_group", "current_task__dispatched_task")
        .prefetch_related(
            "tasks",
            "callbacks",
            "callbacks__callback_service",
            "callbacks__dispatched_task",
        )
    )
    endpoint_name = "workflows"
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
        ],
        "workflow.workflow_viewer": ["workflow.view_workflow"],
    }

    def get_serializer_class(self):
        if self.action == "partial_update":
            return WorkflowCancelSerializer
        return super().get_serializer_class()

    @extend_schema(
        description=(
            "Cancel a workflow. Workflows can be canceled while waiting or running; "
            "canceling a workflow that has already reached a terminal state returns 409."
        ),
        summary="Cancel a workflow",
        operation_id="workflows_cancel",
        responses={200: WorkflowSerializer, 409: WorkflowSerializer},
    )
    def partial_update(self, request, pk=None, partial=True):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        workflow = self.get_object()
        fired_callbacks = False
        with transaction.atomic():
            workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)
            if workflow.state == TASK_STATES.WAITING:
                workflow.state = TASK_STATES.CANCELED
                workflow.finished_at = timezone.now()
                workflow.save(update_fields=["state", "finished_at", "pulp_last_updated"])
                TaskSchedule.objects.filter(name=f"pulp_workflow.workflow:{workflow.pk}").delete()
                if workflow.task_group is not None and not workflow.task_group.all_tasks_dispatched:
                    workflow.task_group.all_tasks_dispatched = True
                    workflow.task_group.save(
                        update_fields=["all_tasks_dispatched", "pulp_last_updated"]
                    )
                fired_callbacks = True
                http_status = None
            elif workflow.state == TASK_STATES.RUNNING:
                workflow.state = TASK_STATES.CANCELED
                workflow.finished_at = timezone.now()
                workflow.save(update_fields=["state", "finished_at", "pulp_last_updated"])
                # Cancel in-flight child tasks and queued continuations only after the
                # outermost transaction commits, so workers don't observe stale state and
                # we don't extend the surrounding transaction.
                if workflow.task_group_id is not None:
                    transaction.on_commit(
                        functools.partial(cancel_task_group, workflow.task_group_id)
                    )
                fired_callbacks = True
                http_status = None
            else:
                http_status = status.HTTP_409_CONFLICT

        if fired_callbacks:
            # Fire CANCELED + FINISHED callbacks. Done outside the select_for_update block so the
            # dispatch can read the workflow's persisted state.
            dispatch_workflow_callbacks(workflow, TASK_STATES.CANCELED)

        out = WorkflowSerializer(workflow, context={"request": request})
        return Response(out.data, status=http_status)


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
