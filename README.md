# pulp-workflow

> **Warning:** This is a community plugin and is not officially supported. Scheduling tasks incorrectly can cause serious issues in your Pulp instance. Always test in a development environment first before applying changes to production.

A Pulp plugin that introduces `TaskPlan` — a named, ordered pipeline of tasks
dispatched sequentially.

A `TaskPlan` owns one or more `TaskPlanStep` rows. Each step records the
`task_name`, `task_args`, `task_kwargs`, and any `reserved_resources` to use
when dispatching that step. Plans are immutable after creation: to change a
plan, delete it and create a new one.

## Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/pulp/api/v3/workflow/task-plans/` | List task plans |
| POST | `/pulp/api/v3/workflow/task-plans/` | Create a task plan (with steps) |
| GET | `/pulp/api/v3/workflow/task-plans/<pk>/` | Retrieve a task plan |
| DELETE | `/pulp/api/v3/workflow/task-plans/<pk>/` | Delete a task plan |

## Installation

```bash
pip install -e ./pulp_workflow
```
