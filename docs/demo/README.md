# Demo: sync + publish a file repo via a Workflow, with a messaging callback

Drives `pulp_workflow` end-to-end: register a `CallbackService` (a script that
POSTs to a webhook, e.g. Discord/Slack), create a `file` repo + remote, POST a
`Workflow` that syncs then publishes with a `finished` callback, and watch it
run.

Assumes a running dev stack (`oci-env compose up`), a configured pulp-cli, and
`NOTIFY_WEBHOOK` available to the Pulp worker. With `oci-env`, add it to
`compose.env` and bounce the stack so the new value is picked up. For testing,
[`httpbin`](https://httpbin.org) returns 200 for any POST:

```bash
echo 'NOTIFY_WEBHOOK=https://httpbin.org/post' >> ../oci_env/compose.env
oci-env compose up -d   # recreate the pulp container so env_file is re-read
```

The demo uses fixed resource names, so run `oci-env pdbreset` between runs to
wipe the Pulp DB.


## 1. Point at the notify script

`oci-env` bind-mounts your `pulp-dev/` checkout at `/src` inside the `pulp`
container, so [notify.sh](notify.sh) is visible to the worker at:

```bash
SCRIPT_PATH=/src/pulp_workflow/docs/demo/notify.sh
oci-env exec ls -la "$SCRIPT_PATH"   # sanity check: file is present and +x
```

## 2. Register the CallbackService

```bash
CB_HREF=$(http POST :5001/pulp/api/v3/workflow/callback-services/ \
    name=demo-messaging-notify \
    script=$SCRIPT_PATH \
    | jq -r .pulp_href)
echo "callback_service: $CB_HREF"
```

## 3. Create the repository and remote

```bash
REPO_NAME=demo-file-repo
REMOTE_NAME=demo-file-remote

pulp file repository create --name "$REPO_NAME"
pulp file remote create \
    --name "$REMOTE_NAME" \
    --url https://fixtures.pulpproject.org/file/PULP_MANIFEST \
    --policy immediate

REPO_HREF=$(pulp file repository show --name "$REPO_NAME" | jq -r .pulp_href)
REMOTE_HREF=$(pulp file remote show --name "$REMOTE_NAME" | jq -r .pulp_href)

# pulpcore tasks take pks, not hrefs, so strip the trailing UUID segment.
REPO_PK=$(echo "$REPO_HREF" | awk -F/ '{print $(NF-1)}')
REMOTE_PK=$(echo "$REMOTE_HREF" | awk -F/ '{print $(NF-1)}')
```

## 4. Create the workflow

The `publish` task's `repository_version_pk` is a *dynamic* arg
(`content_type: core.repositoryversion`) — the workflow engine resolves it at
dispatch time from the previous task's `created_resources`. One `finished`
callback fires on any terminal state.

```bash
WF_HREF=$(http POST :5001/pulp/api/v3/workflow/workflows/ <<JSON | jq -r .pulp_href
{
    "name": "demo-workflow",
    "tasks": [
        {
            "task_name": "pulp_file.app.tasks.synchronizing.synchronize",
            "reserved_resources": ["$REPO_HREF"],
            "task_kwargs": [
                {"kwarg_key": "repository_pk", "value": "$REPO_PK"},
                {"kwarg_key": "remote_pk",     "value": "$REMOTE_PK"},
                {"kwarg_key": "mirror",        "value": false}
            ]
        },
        {
            "task_name": "pulp_file.app.tasks.publish",
            "reserved_resources": ["$REPO_HREF"],
            "task_kwargs": [
                {"kwarg_key": "repository_version_pk",
                 "content_type": "core.repositoryversion"}
            ]
        }
    ],
    "callbacks": [
        {"callback_service": "$CB_HREF", "callback_type": "finished"}
    ]
}
JSON
)
echo "WF_HREF=$WF_HREF"
```

## 5. Watch it run

```bash
RUN_HREF=""
while :; do
    if [ -z "$RUN_HREF" ]; then
        RUN_HREF=$(http :5001"$WF_HREF"runs/ | jq -r '.results[0].pulp_href // empty')
        [ -z "$RUN_HREF" ] && sleep 2 && continue
    fi
    STATE=$(http :5001"$RUN_HREF" | jq -r .state)
    echo "state=$STATE"
    case "$STATE" in
        completed|failed|canceled|skipped) break ;;
    esac
    sleep 2
done

http :5001"$RUN_HREF" | jq '{state, started_at, finished_at, error,
    callbacks: [.callbacks[] | {callback_type, dispatched_task}]}'
```

On success: `state: "completed"`, every `dispatched_task` non-null, and a new
`RepositoryVersion` + `Publication`:

```bash
pulp file repository version list --repository "$REPO_NAME"
pulp file publication list --repository "$REPO_NAME"
```

The callback task ending in `completed` means `curl -fsS` got a 2xx from the
webhook:

```bash
CB_TASK_HREF=$(http :5001"$WF_HREF" | jq -r '.callbacks[0].dispatched_task')
http :5001"$CB_TASK_HREF" | jq '{name, state, error}'
```
