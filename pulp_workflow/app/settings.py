# workaround for: https://github.com/pulp/pulp_rpm/issues/4125
# drf-spectacular emits an empty `{}` schema inside `oneOf` for plain
# ``JSONField`` under OAS 3.1, which the Python openapi-generator turns into a
# bogus model class whose ``to_dict()`` is called on the raw value (crashing for
# str/int). Pinning back to OAS 3.0.1 mirrors what pulp_rpm does and keeps the
# generated bindings working.
SPECTACULAR_SETTINGS__OAS_VERSION = "3.0.1"

# Workflow fields exposed to ``CallbackService`` scripts via ``PULP_WORKFLOW_*`` env vars.
# Allowed: "pk", "name", "state", "labels" (all labels), or "labels:<key>" (one label).
# ``CORRELATION_ID`` is always exposed.
# Only expose more fields if you're certain it won't present a security risk.
WORKFLOW_CALLBACK_FIELDS = ["name", "state"]
