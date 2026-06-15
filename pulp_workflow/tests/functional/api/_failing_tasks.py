"""Test-only tasks dispatched by functional tests to trigger workflow failures.

These tasks raise a ``PulpException`` subclass so the pulpcore worker does not
log a ``pulpcore.deprecation`` warning (the deprecations CI job fails if any
such warning is emitted). They live with the tests rather than in
``pulp_workflow.app.tasks`` because they have no production use and would be
misleading if exposed there.

The module is importable by the pulpcore worker because
``pulp_workflow/tests/`` is included in the installed package.
"""

from pulpcore.exceptions import ValidationError


def fail_with_validation_error(audit_marker=None, **kwargs):
    """Always raise ``ValidationError`` with a constant message.

    All kwargs are accepted and ignored so tests can pass arbitrary sentinel
    values without those values appearing in the resulting exception.
    """
    raise ValidationError("Test task intentionally failed.")
