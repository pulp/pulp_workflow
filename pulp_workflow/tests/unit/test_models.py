from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from pulp_workflow.app.models import CallbackService

# Constructing CallbackService triggers Django's pulp_domain default (get_domain_pk),
# which queries the DB. These tests don't read/write rows, but they need DB access.
pytestmark = pytest.mark.django_db


def _stub_workflow():
    return SimpleNamespace(
        pk="00000000-0000-0000-0000-000000000001",
        name="my-workflow",
        state="completed",
        pulp_labels={"email": "user@example.com"},
    )


def test_callback_env_default_omits_pk_and_labels():
    """Default exposes only name+state; pk and labels are NOT leaked."""
    with override_settings(WORKFLOW_CALLBACK_FIELDS=["name", "state"]):
        env = CallbackService(name="cb", script="/bin/echo")._env(_stub_workflow())
    assert env["PULP_WORKFLOW_NAME"] == "my-workflow"
    assert env["PULP_WORKFLOW_STATE"] == "completed"
    assert "PULP_WORKFLOW_PK" not in env
    assert "PULP_WORKFLOW_LABELS" not in env
    assert "PULP_WORKFLOW_LABEL_EMAIL" not in env


def test_callback_env_labels_field_exposes_per_label_vars():
    """Opting in to ``labels`` exposes both the JSON view and per-label vars."""
    with override_settings(WORKFLOW_CALLBACK_FIELDS=["labels"]):
        env = CallbackService(name="cb", script="/bin/echo")._env(_stub_workflow())
    assert "user@example.com" in env["PULP_WORKFLOW_LABELS"]
    assert env["PULP_WORKFLOW_LABEL_EMAIL"] == "user@example.com"
    assert "PULP_WORKFLOW_NAME" not in env


def test_callback_env_unknown_field_raises():
    """A typo in the setting fails loudly rather than silently leaking/dropping data."""
    with override_settings(WORKFLOW_CALLBACK_FIELDS=["secrets"]):
        with pytest.raises(ImproperlyConfigured, match="secrets"):
            CallbackService(name="cb", script="/bin/echo")._env(_stub_workflow())


def test_callback_env_labels_key_exposes_only_that_label():
    """``labels:<key>`` exposes a single label and does NOT leak the JSON view or others."""
    workflow = SimpleNamespace(
        pk="x",
        name="w",
        state="completed",
        pulp_labels={"email": "user@example.com", "secret": "shh"},
    )
    with override_settings(WORKFLOW_CALLBACK_FIELDS=["labels:email"]):
        env = CallbackService(name="cb", script="/bin/echo")._env(workflow)
    assert env["PULP_WORKFLOW_LABEL_EMAIL"] == "user@example.com"
    assert "PULP_WORKFLOW_LABEL_SECRET" not in env
    assert "PULP_WORKFLOW_LABELS" not in env


def test_callback_env_labels_key_missing_label_is_empty_string():
    """Requested label that the workflow doesn't have still gets a (empty) env var."""
    with override_settings(WORKFLOW_CALLBACK_FIELDS=["labels:absent"]):
        env = CallbackService(name="cb", script="/bin/echo")._env(_stub_workflow())
    assert env["PULP_WORKFLOW_LABEL_ABSENT"] == ""


def test_callback_env_labels_empty_key_raises():
    """``labels:`` with no key is a typo, not a valid request."""
    with override_settings(WORKFLOW_CALLBACK_FIELDS=["labels:"]):
        with pytest.raises(ImproperlyConfigured, match="labels:"):
            CallbackService(name="cb", script="/bin/echo")._env(_stub_workflow())
