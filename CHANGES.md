# Changelog

[//]: # (You should *NOT* be adding new change log entries to this file, this)
[//]: # (file is managed by towncrier. You *may* edit previous change logs to)
[//]: # (fix problems like typo corrections or such.)
[//]: # (To add a new change log entry, please see the contributing docs.)
[//]: # (WARNING: Don't drop the towncrier directive!)

[//]: # (towncrier release notes start)

## 0.3.0 (2026-07-23) {: #0.3.0 }

#### Features {: #0.3.0-feature }

- Workflows can now set a `dispatch_interval` to re-run on a recurring schedule. Added a `WorkflowRun`
  resource (`/pulp/api/v3/workflow/workflow-runs/`), which tracks each execution of a workflow.
  [#20](https://github.com/pulp/pulp_workflow/issues/20)

#### Removals {: #0.3.0-removal }

- Moved per-execution state off of the `Workflow` resource. The `state`, `started_at`,
  `finished_at`, `error`, `current_task`, and `task_group` fields now live on the new
  `WorkflowRun` resource. Support for workflow cancellation callbacks has also been removed for now.
  [#20](https://github.com/pulp/pulp_workflow/issues/20)

---

## 0.2.1 (2026-07-17) {: #0.2.1 }

No significant changes.

---

## 0.2.0 (2026-06-23) {: #0.2.0 }

#### Features {: #0.2.0-feature }

- Added a `pulpcore-manager add-callback-service` management command for registering a `CallbackService` from the worker host (mirrors pulpcore's `add-signing-service`). Useful for image bootstrap scripts that need to register a callback before the API serves traffic.
  [#30](https://github.com/pulp/pulp_workflow/issues/30)
- Added `list-callback-services` and `remove-callback-service` management commands for inspecting and removing registered `CallbackService` rows, mirroring pulpcore's `list-signing-services` and `remove-signing-service`.
  [#36](https://github.com/pulp/pulp_workflow/issues/36)

---

## 0.1.1 (2026-06-08) {: #0.1.1 }

No significant changes.

---

## 0.1.0 (2026-06-08) {: #0.1.0 }

#### Features {: #0.1.0-feature }

- Allowed canceling a Workflow after it has started running. PATCH
  ``{"state": "canceled"}`` now cancels in-flight child tasks and queued
  continuations via the workflow's ``TaskGroup``; canceling the
  ``TaskGroup`` directly also propagates ``CANCELED`` to the ``Workflow``.
  [#4](https://github.com/pulp/pulp_workflow/issues/4)
- Added user-registered callbacks that fire on `Workflow` lifecycle events. A new `CallbackService`
  resource (modeled after pulpcore's `SigningService`) points at an absolute path to an executable; it
  is attached to a workflow via a `WorkflowCallback` whose `callback_type` selects the event
  (`running`, `completed`, etc). The script runs as a Pulp task with workflow context available as
  environment variables.
  [#10](https://github.com/pulp/pulp_workflow/issues/10)
- Added support for ``pulp_labels`` on Workflows. Labels can be set at create
  time, updated via the ``set_label``/``unset_label`` actions, and used to
  filter the list endpoint with ``pulp_label_select``.
- Associated every Workflow with a pulpcore ``TaskGroup``, exposed via a new
  read-only ``task_group`` field on the Workflow resource.
- Reworked the `Workflow` API: server-assigned `WorkflowTask.index` and
  `WorkflowTaskArg.arg_index`, hyperlinked `Workflow.current_task`, and
  mutually-exclusive `value`/`content_type` on task args (replacing the
  removed `dynamic` flag). Added expanded list filters via `BaseFilterSet`
  (`pulp_href`, `prn`, `q`, name/state lookups, datetime ranges).

#### Bugfixes {: #0.1.0-bugfix }

- Fixed an `AttributeError` in `execute_workflow` when a previous step's
  dispatched `core.Task` had been deleted between dispatch and the
  continuation (e.g. by `orphan_cleanup`). The workflow is now transitioned
  to `failed` with a clear description in that case.
- Pinned the generated OpenAPI spec to 3.0.1 to work around an
  openapi-generator bug that produces broken Python bindings for plain
  `JSONField` under OAS 3.1.

---
