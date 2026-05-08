import uuid
from contextlib import suppress
from time import sleep

import pytest

from pulpcore.tests.functional.utils import BindingsNamespace

# Mirrors the constants pulpcore's monitor_task fixture uses.
WORKFLOW_TIMEOUT = 30 * 60
WORKFLOW_SLEEP_TIME = 2.0
WORKFLOW_FINAL_STATES = {"completed", "failed", "canceled", "skipped"}


class WorkflowError(Exception):
    """Raised when a Workflow reaches a non-completed final state."""

    def __init__(self, workflow):
        self.workflow = workflow
        super().__init__(
            f"Workflow {workflow.pulp_href} ended in state "
            f"{workflow.state!r}: error={workflow.error!r}"
        )


class WorkflowTimeoutError(Exception):
    """Raised when a Workflow does not reach a final state in the timeout."""

    def __init__(self, workflow):
        self.workflow = workflow
        super().__init__(
            f"Workflow {workflow.pulp_href} did not reach a final state in time "
            f"(state={workflow.state!r})"
        )


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


@pytest.fixture
def callback_service_factory(workflow_bindings):
    """A factory to generate a CallbackService with auto-cleanup at teardown.

    By default the script is ``/bin/echo`` so the callback always succeeds and is safe to invoke
    many times. Override with a ``script`` kwarg to point at a different absolute path.
    """

    created = []

    def _create_callback_service(**kwargs):
        kwargs.setdefault("name", str(uuid.uuid4()))
        kwargs.setdefault("script", "/bin/echo")
        cs = workflow_bindings.CallbackServicesApi.create(kwargs)
        created.append(cs.pulp_href)
        return cs

    yield _create_callback_service

    for href in reversed(created):
        with suppress(Exception):
            workflow_bindings.CallbackServicesApi.delete(href)


@pytest.fixture(scope="session")
def monitor_workflow(workflow_bindings):
    """Wait for a Workflow to reach a final state.

    Mirrors pulpcore's ``monitor_task`` fixture: returns the Workflow in ``completed`` state,
    raises ``WorkflowTimeoutError`` if the timeout in seconds (defaulting to 30*60) is exceeded,
    or raises ``WorkflowError`` if it reached any other final state.
    """

    def _monitor_workflow(workflow_href, timeout=WORKFLOW_TIMEOUT):
        # Always make at least one read attempt, even if the timeout is shorter than the sleep
        # interval, so ``workflow`` is bound before the ``else`` branch can reference it.
        attempts = max(1, int(timeout / WORKFLOW_SLEEP_TIME))
        for _ in range(attempts):
            workflow = workflow_bindings.WorkflowsApi.read(workflow_href)
            if workflow.state in WORKFLOW_FINAL_STATES:
                break
            sleep(WORKFLOW_SLEEP_TIME)
        else:
            raise WorkflowTimeoutError(workflow)

        if workflow.state != "completed":
            raise WorkflowError(workflow)
        return workflow

    return _monitor_workflow
