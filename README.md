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

## Design

For details on how workflows execute, integrate with pulpcore `TaskGroup`s,
and handle cancellation, see the [design doc](https://github.com/daviddavis/pulp_workflow/blob/main/docs/design.md).
