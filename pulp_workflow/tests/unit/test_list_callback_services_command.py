import pytest
from django.core.management import call_command

from pulp_workflow.app.models import CallbackService

pytestmark = pytest.mark.django_db


def test_list_callback_services_empty(capsys):
    call_command("list-callback-services")
    assert capsys.readouterr().out.strip() == ""


def test_list_callback_services_returns_names(tmp_path, capsys):
    script = tmp_path / "cb.sh"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)
    CallbackService.objects.create(name="cb-a", script=str(script))
    CallbackService.objects.create(name="cb-b", script=str(script))

    call_command("list-callback-services")
    out = capsys.readouterr().out.splitlines()
    assert set(out) == {"cb-a", "cb-b"}
