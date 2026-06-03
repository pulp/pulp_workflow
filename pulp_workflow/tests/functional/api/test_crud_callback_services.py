"""CRUD tests for the CallbackService endpoint."""

import uuid

import pytest


@pytest.mark.parallel
def test_create_callback_service(workflow_bindings, callback_service_factory):
    """A CallbackService can be created with a name and absolute script path."""
    name = str(uuid.uuid4())
    cs = callback_service_factory(name=name, script="/bin/echo")
    assert cs.pulp_href is not None
    assert cs.name == name
    assert cs.script == "/bin/echo"


@pytest.mark.parallel
def test_create_callback_service_rejects_relative_script(workflow_bindings):
    """The script must be an absolute path."""
    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.CallbackServicesApi.create({"name": str(uuid.uuid4()), "script": "echo"})
    assert exc.value.status == 400


@pytest.mark.parallel
def test_create_callback_service_rejects_missing_script(workflow_bindings):
    """The script must point at an existing executable."""
    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.CallbackServicesApi.create(
            {"name": str(uuid.uuid4()), "script": "/nonexistent/path/to/script"}
        )
    assert exc.value.status == 400


@pytest.mark.parallel
def test_create_duplicate_callback_service_name_fails(workflow_bindings, callback_service_factory):
    """Names are unique."""
    name = str(uuid.uuid4())
    callback_service_factory(name=name)
    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.CallbackServicesApi.create({"name": name, "script": "/bin/echo"})
    assert exc.value.status == 400


@pytest.mark.parallel
def test_read_callback_service(workflow_bindings, callback_service_factory):
    """A created CallbackService can be retrieved by href."""
    cs = callback_service_factory()
    fetched = workflow_bindings.CallbackServicesApi.read(cs.pulp_href)
    assert fetched.pulp_href == cs.pulp_href
    assert fetched.name == cs.name


@pytest.mark.parallel
def test_list_callback_services(workflow_bindings, callback_service_factory):
    """Listing and filtering CallbackServices."""
    name = str(uuid.uuid4())
    callback_service_factory(name=name)
    results = workflow_bindings.CallbackServicesApi.list(name=name)
    assert results.count == 1
    assert results.results[0].name == name


@pytest.mark.parallel
def test_delete_callback_service(workflow_bindings, callback_service_factory):
    """An unattached CallbackService can be deleted."""
    cs = callback_service_factory()
    workflow_bindings.CallbackServicesApi.delete(cs.pulp_href)
    with pytest.raises(workflow_bindings.ApiException) as exc:
        workflow_bindings.CallbackServicesApi.read(cs.pulp_href)
    assert exc.value.status == 404
