#!/bin/bash
# Demo callback script for `pulp_workflow`. Invoked by a `CallbackService` as a
# subprocess on the Pulp worker; reads workflow context from `PULP_WORKFLOW_*`
# env vars and `NOTIFY_WEBHOOK` from the worker's environment (set via
# oci_env/compose.env in the dev stack).
set -euo pipefail
: "${NOTIFY_WEBHOOK:?NOTIFY_WEBHOOK must be set in the worker environment}"
PAYLOAD=$(jq -nc \
    --arg name  "${PULP_WORKFLOW_NAME:-?}" \
    --arg state "${PULP_WORKFLOW_STATE:-?}" \
    --arg cid   "${CORRELATION_ID:-?}" \
    '{content: ("Workflow \($name) finished in state \($state).")}')
curl -fsS -H 'Content-Type: application/json' -d "$PAYLOAD" "$NOTIFY_WEBHOOK" >/dev/null
