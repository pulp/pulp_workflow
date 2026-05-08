Reworked the `Workflow` API: server-assigned `WorkflowTask.index` and
`WorkflowTaskArg.arg_index`, hyperlinked `Workflow.current_task`, and
mutually-exclusive `value`/`content_type` on task args (replacing the
removed `dynamic` flag). Added expanded list filters via `BaseFilterSet`
(`pulp_href`, `prn`, `q`, name/state lookups, datetime ranges).
