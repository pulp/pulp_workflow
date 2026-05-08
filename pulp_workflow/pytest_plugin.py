import uuid
from contextlib import suppress

import pytest

from pulpcore.tests.functional.utils import BindingsNamespace


@pytest.fixture(scope="session")
def workflow_bindings(_api_client_set, bindings_cfg):
    """
    A namespace providing preconfigured pulp_workflow api clients.

    e.g. `workflow_bindings.WorkflowsApi.list()`.
    """
    from pulpcore.client import pulp_workflow as workflow_bindings_module

    api_client = workflow_bindings_module.ApiClient(bindings_cfg)
    _api_client_set.add(api_client)
    yield BindingsNamespace(workflow_bindings_module, api_client)
    _api_client_set.remove(api_client)


@pytest.fixture
def workflow_factory(workflow_bindings):
    """A factory to generate a Workflow.

    The default workflow runs ``orphan_cleanup`` with a very large
    ``orphan_protection_time`` so it is effectively a no-op and does not race
    with content created by other tests in the same session.

    Best-effort cleanup attempts to cancel the workflow at teardown; canceling only
    succeeds while the workflow is still in the ``waiting`` state, so workflows that
    have started executing are simply left in their final state.
    """

    created = []

    def _create_workflow(**kwargs):
        kwargs.setdefault("name", str(uuid.uuid4()))
        kwargs.setdefault(
            "tasks",
            [
                {
                    "task_name": "pulpcore.app.tasks.orphan_cleanup",
                    "task_kwargs": [
                        # Effectively never delete anything: orphan must be
                        # older than ~1 year before it is eligible.
                        {"kwarg_key": "orphan_protection_time", "value": 525600},
                    ],
                },
            ],
        )
        workflow = workflow_bindings.WorkflowsApi.create(kwargs)
        created.append(workflow.pulp_href)
        return workflow

    yield _create_workflow

    for href in reversed(created):
        with suppress(Exception):
            workflow_bindings.WorkflowsApi.partial_update(href, {"state": "canceled"})
