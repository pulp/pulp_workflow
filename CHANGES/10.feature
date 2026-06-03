Added user-registered callbacks that fire on `Workflow` lifecycle events. A new `CallbackService`
resource (modeled after pulpcore's `SigningService`) points at an absolute path to an executable; it
is attached to a workflow via a `WorkflowCallback` whose `callback_type` selects the event
(`running`, `completed`, etc). The script runs as a Pulp task with workflow context available as
environment variables.
