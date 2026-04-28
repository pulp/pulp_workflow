# pulp-workflow

> **Warning:** This is a community plugin and is not officially supported. Scheduling tasks incorrectly can cause serious issues in your Pulp instance. Always test in a development environment first before applying changes to production.

A Pulp plugin that introduces `Workflow` — a named, ordered pipeline of tasks
dispatched sequentially.

A `Workflow` owns one or more `WorkflowTask` rows. Each task records the
`task_name`, `task_args`, `task_kwargs`, and any `reserved_resources` to use
when dispatching it. Workflows are immutable after creation: to change a
workflow, cancel it (if it has not yet started) and create a new one.

## Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/pulp/api/v3/workflows/` | List workflows |
| POST | `/pulp/api/v3/workflows/` | Create a workflow (with tasks) |
| GET | `/pulp/api/v3/workflows/<pk>/` | Retrieve a workflow |
| PATCH | `/pulp/api/v3/workflows/<pk>/` | Cancel a waiting workflow (body: `{"state": "canceled"}`). Returns 409 if the workflow has already started; only `"canceled"` is accepted as the target state. |

## Installation

```bash
pip install -e ./pulp_workflow
```
