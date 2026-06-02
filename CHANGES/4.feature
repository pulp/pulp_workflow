Allowed canceling a Workflow after it has started running. PATCH
``{"state": "canceled"}`` now cancels in-flight child tasks and queued
continuations via the workflow's ``TaskGroup``; canceling the
``TaskGroup`` directly also propagates ``CANCELED`` to the ``Workflow``.
