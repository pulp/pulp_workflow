from pulp_workflow.app.viewsets import CallbackServiceViewSet, WorkflowViewSet


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
