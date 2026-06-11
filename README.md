# pulp-workflow

> **Warning:** This is a community plugin and is not officially supported. Scheduling tasks incorrectly can cause serious issues in your Pulp instance. Always test in a development environment first before applying changes to production.

A Pulp plugin that introduces the `Workflow` model. Workflows build on top of
tasks in Pulp allowing users to:
* Schedule tasks to run at any given time
* Run sequences of tasks in a specific order
* Set up callback services to run on workflow lifecycle events (e.g. running,
completed, failed, canceled, finished)

A `Workflow` owns one or more `WorkflowTask` rows. Each task records the
`task_name`, `task_args`, `task_kwargs`, and any `reserved_resources` to use
when dispatching it. Workflows are immutable after creation: to change a
workflow, cancel it (if it has not yet started) and create a new one.

## Demo

The demo walks through syncing and publishing a file repo via a Workflow, with
a callback that notifies a messaging service (e.g. Discord/Slack) on
completion. Watch the [YouTube demo](https://www.youtube.com/watch?v=Cqkh_DUPefY)
for a video walkthrough, or follow the [written demo guide](https://github.com/daviddavis/pulp_workflow/blob/main/docs/demo/README.md)
to run it yourself end-to-end.

## Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/pulp/api/v3/workflow/workflows/` | List workflows |
| POST | `/pulp/api/v3/workflow/workflows/` | Create a workflow (with tasks) |
| GET | `/pulp/api/v3/workflow/workflows/<pk>/` | Retrieve a workflow |
| PATCH | `/pulp/api/v3/workflow/workflows/<pk>/` | Cancel a workflow (body: `{"state": "canceled"}`). Works whether the workflow is `waiting` or `running`; returns 409 only if the workflow is already in a terminal state. Only `"canceled"` is accepted as the target state. |
| GET | `/pulp/api/v3/workflow/callback-services/` | List callback services |
| POST | `/pulp/api/v3/workflow/callback-services/` | Register a callback service (an executable on the worker host) |
| GET | `/pulp/api/v3/workflow/callback-services/<pk>/` | Retrieve a callback service |
| PUT, PATCH | `/pulp/api/v3/workflow/callback-services/<pk>/` | Update a callback service |
| DELETE | `/pulp/api/v3/workflow/callback-services/<pk>/` | Delete a callback service |

## Callbacks

A `CallbackService` is a registered executable on the Pulp worker host that
can be attached to a workflow to run on lifecycle events (`running`,
`completed`, `failed`, `canceled`, or the synthetic `finished` event that
fires on any terminal state).

`CallbackService`s can be registered via the API (see
[Endpoints](#endpoints)) or, for image-bootstrap scenarios where a callback
needs to exist before the API serves traffic, via the
`add-callback-service` management command:

```bash
pulpcore-manager add-callback-service <name> <script-path>
```

The command resolves the script path, runs the same validation as the API
(absolute path, file exists, executable bit set), and persists the
`CallbackService` row. Names must be unique within a domain; re-running with
the same name fails with a clear error rather than silently updating, so
bootstrap scripts that may run more than once should guard the call:

```bash
existing=$(pulpcore-manager shell -c \
    "from pulp_workflow.app.models import CallbackService; \
     print(' '.join(CallbackService.objects.values_list('name', flat=True)))")
if [[ $existing != *"my-callback"* ]]; then
    pulpcore-manager add-callback-service my-callback /path/to/script.sh
fi
```

## Design

For details on how workflows execute, integrate with pulpcore `TaskGroup`s,
and handle cancellation, see the [design doc](https://github.com/daviddavis/pulp_workflow/blob/main/docs/design.md).
