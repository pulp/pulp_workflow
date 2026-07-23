from pulp_workflow.app.viewsets import (
    CallbackServiceViewSet,
    WorkflowRunListViewSet,
    WorkflowRunViewSet,
    WorkflowViewSet,
)


def test_access_policy_requires_view_for_read():
    """Read actions require view_workflow permission."""
    policy = WorkflowViewSet.DEFAULT_ACCESS_POLICY
    read_stmt = policy["statements"][0]
    assert set(read_stmt["action"]) == {"list", "retrieve", "my_permissions"}
    assert "view_workflow" in read_stmt["condition"]


def test_access_policy_requires_change_for_write():
    """Write actions require change_workflow permission."""
    policy = WorkflowViewSet.DEFAULT_ACCESS_POLICY
    write_stmt = policy["statements"][1]
    assert "create" in write_stmt["action"]
    assert "partial_update" in write_stmt["action"]
    assert "change_workflow" in write_stmt["condition"]


def test_access_policy_does_not_expose_destroy():
    """The destroy endpoint has been removed in favor of cancel (partial_update)."""
    policy = WorkflowViewSet.DEFAULT_ACCESS_POLICY
    all_actions = {action for stmt in policy["statements"] for action in stmt["action"]}
    assert "destroy" not in all_actions


def test_locked_roles():
    """Admin and viewer roles are defined."""
    roles = WorkflowViewSet.LOCKED_ROLES
    assert "workflow.workflow_admin" in roles
    assert "workflow.workflow_viewer" in roles
    assert "workflow.view_workflow" in roles["workflow.workflow_viewer"]
    assert "workflow.change_workflow" in roles["workflow.workflow_admin"]
    # delete_workflow is no longer assigned to any locked role since the destroy endpoint
    # has been removed.
    for perms in roles.values():
        assert "workflow.delete_workflow" not in perms


def test_locked_roles_include_run_permissions():
    """Run view/change permissions are bundled into the workflow roles."""
    roles = WorkflowViewSet.LOCKED_ROLES
    assert "workflow.view_workflowrun" in roles["workflow.workflow_viewer"]
    assert "workflow.view_workflowrun" in roles["workflow.workflow_admin"]
    assert "workflow.change_workflowrun" in roles["workflow.workflow_admin"]
    # A plain viewer must not be able to cancel (change) runs.
    assert "workflow.change_workflowrun" not in roles["workflow.workflow_viewer"]


def test_run_access_policy_requires_view_for_read():
    """WorkflowRun read actions require view_workflowrun permission."""
    policy = WorkflowRunViewSet.DEFAULT_ACCESS_POLICY
    read_stmt = policy["statements"][0]
    assert set(read_stmt["action"]) == {"list", "retrieve", "my_permissions"}
    assert "view_workflowrun" in read_stmt["condition"]


def test_run_access_policy_requires_change_for_cancel():
    """Canceling a run (partial_update) requires change_workflowrun permission."""
    policy = WorkflowRunViewSet.DEFAULT_ACCESS_POLICY
    write_stmt = policy["statements"][1]
    assert write_stmt["action"] == ["partial_update"]
    assert "change_workflowrun" in write_stmt["condition"]


def test_run_viewset_is_read_and_cancel_only():
    """The run viewset exposes no create/destroy actions."""
    from rest_framework import mixins

    assert not issubclass(WorkflowRunViewSet, mixins.CreateModelMixin)
    assert not issubclass(WorkflowRunViewSet, mixins.DestroyModelMixin)
    assert issubclass(WorkflowRunViewSet, mixins.ListModelMixin)
    assert issubclass(WorkflowRunViewSet, mixins.RetrieveModelMixin)


def test_run_viewset_is_nested_under_workflows():
    """The canonical run viewset is nested under workflows."""
    assert WorkflowRunViewSet.parent_viewset is WorkflowViewSet
    assert WorkflowRunViewSet.parent_lookup_kwargs == {"workflow_pk": "workflow__pk"}
    assert WorkflowRunViewSet.endpoint_name == "runs"


def test_flat_run_list_viewset_is_list_only():
    """The flat top-level run collection exposes only list (no retrieve/cancel/create/destroy)."""
    from rest_framework import mixins

    assert issubclass(WorkflowRunListViewSet, mixins.ListModelMixin)
    assert not issubclass(WorkflowRunListViewSet, mixins.RetrieveModelMixin)
    assert not issubclass(WorkflowRunListViewSet, mixins.CreateModelMixin)
    assert not issubclass(WorkflowRunListViewSet, mixins.DestroyModelMixin)
    # It is a flat, non-nested endpoint at /workflow/workflow-runs/.
    assert WorkflowRunListViewSet.endpoint_name == "workflow-runs"
    assert getattr(WorkflowRunListViewSet, "parent_viewset", None) is None


def test_flat_run_list_access_policy_requires_view_and_only_lists():
    """The flat run list only allows list, gated on view_workflowrun."""
    policy = WorkflowRunListViewSet.DEFAULT_ACCESS_POLICY
    all_actions = {action for stmt in policy["statements"] for action in stmt["action"]}
    assert all_actions == {"list"}
    assert "view_workflowrun" in policy["statements"][0]["condition"]


def test_flat_run_list_uses_distinct_binding_tag():
    """The flat and nested run endpoints must land in different binding API classes.

    They share the ``list`` action, so an identical tag would collide the generated ``list``
    methods in one API class.
    """
    assert WorkflowRunListViewSet.pulp_tag_name != WorkflowRunViewSet.pulp_tag_name


def test_callback_service_access_policy_requires_view_for_read():
    """CallbackService read actions require view_callbackservice permission."""
    policy = CallbackServiceViewSet.DEFAULT_ACCESS_POLICY
    read_stmt = policy["statements"][0]
    assert set(read_stmt["action"]) == {"list", "retrieve", "my_permissions"}
    assert "view_callbackservice" in read_stmt["condition"]


def test_callback_service_access_policy_distinguishes_write_actions():
    """add/change/delete on CallbackService each require their own permission."""
    statements = {
        tuple(s["action"]): s for s in CallbackServiceViewSet.DEFAULT_ACCESS_POLICY["statements"]
    }
    assert "add_callbackservice" in statements[("create",)]["condition"]
    assert "change_callbackservice" in statements[("update", "partial_update")]["condition"]
    assert "delete_callbackservice" in statements[("destroy",)]["condition"]


def test_callback_service_locked_roles():
    """Admin and viewer roles are defined for CallbackService."""
    roles = CallbackServiceViewSet.LOCKED_ROLES
    assert "workflow.callbackservice_admin" in roles
    assert "workflow.callbackservice_viewer" in roles
    admin_perms = roles["workflow.callbackservice_admin"]
    assert "workflow.add_callbackservice" in admin_perms
    assert "workflow.change_callbackservice" in admin_perms
    assert "workflow.delete_callbackservice" in admin_perms
    assert "workflow.view_callbackservice" in admin_perms
    assert "workflow.manage_roles_callbackservice" in admin_perms
    assert roles["workflow.callbackservice_viewer"] == ["workflow.view_callbackservice"]
