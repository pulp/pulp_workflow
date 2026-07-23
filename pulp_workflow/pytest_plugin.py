import uuid
from contextlib import suppress
from time import monotonic as time_monotonic
from time import sleep

import pytest

from pulpcore.tests.functional.utils import BindingsNamespace

# Mirrors the constants pulpcore's monitor_task fixture uses.
WORKFLOW_TIMEOUT = 30 * 60
WORKFLOW_SLEEP_TIME = 2.0
WORKFLOW_FINAL_STATES = {"completed", "failed", "canceled", "skipped"}


class WorkflowRunError(Exception):
    """Raised when a WorkflowRun reaches a non-completed final state."""

    def __init__(self, run):
        self.run = run
        super().__init__(
            f"WorkflowRun {run.pulp_href} ended in state {run.state!r}: error={run.error!r}"
        )


class WorkflowRunTimeoutError(Exception):
    """Raised when a WorkflowRun does not reach a final state in the timeout."""

    def __init__(self, run):
        self.run = run
        super().__init__(
            f"WorkflowRun {run.pulp_href} did not reach a final state in time (state={run.state!r})"
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
def workflow_runs(workflow_bindings):
    """Return the WorkflowRun results for a workflow, newest first."""

    def _workflow_runs(workflow_href):
        return workflow_bindings.WorkflowRunsApi.list(workflow_href).results

    return _workflow_runs


@pytest.fixture(scope="session")
def monitor_workflow(workflow_bindings, workflow_runs):
    """Wait for a Workflow's run to reach a final state and return that run.

    A Workflow is only a definition; its execution state lives on a ``WorkflowRun`` created
    when the schedule fires. This first waits for the run to appear, then polls it. Mirrors
    pulpcore's ``monitor_task`` fixture: returns the run in ``completed`` state, raises
    ``WorkflowRunTimeoutError`` if the timeout in seconds (defaulting to 30*60) is exceeded, or
    raises ``WorkflowRunError`` if it reached any other final state.
    """

    def _monitor_workflow(workflow_href, timeout=WORKFLOW_TIMEOUT):
        deadline = time_monotonic() + timeout
        # First, wait for the scheduler to create a run for this workflow.
        run = None
        while time_monotonic() < deadline:
            runs = workflow_runs(workflow_href)
            if runs:
                run = runs[0]
                break
            sleep(WORKFLOW_SLEEP_TIME)
        if run is None:
            raise WorkflowRunTimeoutError(
                type("_NoRun", (), {"pulp_href": workflow_href, "state": None, "error": None})()
            )

        # Then wait for that run to reach a final state.
        while run.state not in WORKFLOW_FINAL_STATES:
            if time_monotonic() >= deadline:
                raise WorkflowRunTimeoutError(run)
            sleep(WORKFLOW_SLEEP_TIME)
            run = workflow_bindings.WorkflowRunsApi.read(run.pulp_href)

        if run.state != "completed":
            raise WorkflowRunError(run)
        return run

    return _monitor_workflow
