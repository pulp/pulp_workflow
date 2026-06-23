import pytest
from django.core.management import CommandError, call_command

from pulp_workflow.app.models import CallbackService

pytestmark = pytest.mark.django_db


def _make_script(tmp_path):
    script = tmp_path / "notify.sh"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)
    return script


def test_remove_callback_service_deletes_row(tmp_path):
    """Happy path: command deletes the matching CallbackService row."""
    script = _make_script(tmp_path)
    CallbackService.objects.create(name="demo-cb", script=str(script))

    call_command("remove-callback-service", "demo-cb")

    assert not CallbackService.objects.filter(name="demo-cb").exists()


def test_remove_callback_service_in_use_raises(tmp_path):
    """A CallbackService referenced by a WorkflowCallback cannot be removed (on_delete=PROTECT)."""
    from pulp_workflow.app.models import CALLBACK_TYPES, Workflow, WorkflowCallback

    script = _make_script(tmp_path)
    cb = CallbackService.objects.create(name="in-use", script=str(script))
    wf = Workflow.objects.create(name="wf-protects-cb")
    WorkflowCallback.objects.create(
        workflow=wf,
        callback_service=cb,
        callback_type=CALLBACK_TYPES.FINISHED,
    )

    with pytest.raises(CommandError, match="still referenced"):
        call_command("remove-callback-service", "in-use")

    assert CallbackService.objects.filter(name="in-use").exists()


def test_remove_callback_service_missing_name_raises():
    """Removing a name that doesn't exist raises a clear CommandError."""
    with pytest.raises(CommandError, match="No callback service named 'demo-cb'"):
        call_command("remove-callback-service", "demo-cb")


def test_remove_callback_service_only_removes_named_row(tmp_path):
    """Other CallbackService rows are unaffected by a targeted remove."""
    script = _make_script(tmp_path)
    CallbackService.objects.create(name="keep-me", script=str(script))
    CallbackService.objects.create(name="drop-me", script=str(script))

    call_command("remove-callback-service", "drop-me")

    assert CallbackService.objects.filter(name="keep-me").exists()
    assert not CallbackService.objects.filter(name="drop-me").exists()


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_remove_callback_service_blank_name_raises(name):
    """Mirror add-callback-service's non-blank `name` requirement."""
    with pytest.raises(CommandError, match="non-blank"):
        call_command("remove-callback-service", name)
