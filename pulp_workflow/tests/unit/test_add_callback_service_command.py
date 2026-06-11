import pytest
from django.core.management import CommandError, call_command

from pulp_workflow.app.models import CallbackService

pytestmark = pytest.mark.django_db


def test_add_callback_service_creates_row(tmp_path):
    """Happy path: command creates a CallbackService row pointing at the script."""
    script = tmp_path / "notify.sh"
    script.write_text("#!/bin/bash\necho hi\n")
    script.chmod(0o755)

    call_command("add-callback-service", "demo-cb", str(script))

    obj = CallbackService.objects.get(name="demo-cb")
    assert obj.script == str(script.resolve())


def test_add_callback_service_resolves_relative_path(tmp_path, monkeypatch):
    """A relative path is resolved to an absolute path before being persisted."""
    script = tmp_path / "notify.sh"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)
    monkeypatch.chdir(tmp_path)

    call_command("add-callback-service", "demo-cb", "notify.sh")

    obj = CallbackService.objects.get(name="demo-cb")
    assert obj.script == str(script.resolve())


def test_add_callback_service_missing_script_raises(tmp_path):
    """A non-existent path is rejected by Path.resolve(strict=True)."""
    missing = tmp_path / "nope.sh"
    with pytest.raises(CommandError, match="nope.sh"):
        call_command("add-callback-service", "demo-cb", str(missing))
    assert not CallbackService.objects.filter(name="demo-cb").exists()


def test_add_callback_service_non_executable_raises(tmp_path):
    """The model's validate() rejects a non-executable script; surfaced as CommandError."""
    script = tmp_path / "notify.sh"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o644)

    with pytest.raises(CommandError, match="not executable"):
        call_command("add-callback-service", "demo-cb", str(script))
    assert not CallbackService.objects.filter(name="demo-cb").exists()


def test_add_callback_service_duplicate_name_raises(tmp_path):
    """The unique_together(pulp_domain, name) constraint surfaces as a CommandError."""
    script = tmp_path / "notify.sh"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)

    call_command("add-callback-service", "demo-cb", str(script))
    with pytest.raises(CommandError, match="already exists"):
        call_command("add-callback-service", "demo-cb", str(script))
    assert CallbackService.objects.filter(name="demo-cb").count() == 1


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_add_callback_service_blank_name_raises(tmp_path, name):
    """Mirror the API serializer's non-blank `name` requirement."""
    script = tmp_path / "notify.sh"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)

    with pytest.raises(CommandError, match="non-blank"):
        call_command("add-callback-service", name, str(script))
    assert not CallbackService.objects.exists()
