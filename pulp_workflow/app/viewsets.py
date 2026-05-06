from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status
from rest_framework.response import Response

from pulpcore.plugin.constants import TASK_STATES
from pulpcore.plugin.models import TaskSchedule
from pulpcore.plugin.viewsets import NamedModelViewSet, RolesMixin

from pulp_workflow.app.models import Workflow
from pulp_workflow.app.serializers import WorkflowCancelSerializer, WorkflowSerializer


class WorkflowViewSet(
    NamedModelViewSet,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    RolesMixin,
):
    """
    A ViewSet for managing Workflows.

    Workflows are created with their full set of tasks and are immutable thereafter; to
    change a workflow, cancel it (if it has not yet started) and create a new one.
    """

    queryset = Workflow.objects.all().prefetch_related("tasks")
    endpoint_name = "workflows"
    serializer_class = WorkflowSerializer
    ordering = "-pulp_created"
    filterset_fields = {
        "name": ["exact", "contains"],
        "state": ["exact", "in"],
    }
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
            "Cancel a workflow. A workflow can only be canceled before it has started "
            "executing; otherwise this returns 409 Conflict."
        ),
        summary="Cancel a workflow",
        operation_id="workflows_cancel",
        responses={200: WorkflowSerializer, 409: WorkflowSerializer},
    )
    def partial_update(self, request, pk=None, partial=True):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        workflow = self.get_object()
        with transaction.atomic():
            workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)
            if workflow.state == TASK_STATES.WAITING:
                workflow.state = TASK_STATES.CANCELED
                workflow.finished_at = timezone.now()
                workflow.save(update_fields=["state", "finished_at", "pulp_last_updated"])
                TaskSchedule.objects.filter(name=f"pulp_workflow.workflow:{workflow.pk}").delete()
                http_status = None
            else:
                http_status = status.HTTP_409_CONFLICT

        out = WorkflowSerializer(workflow, context={"request": request})
        return Response(out.data, status=http_status)
