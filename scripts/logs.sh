#!/usr/bin/env bash
# Tail the Waymark CloudWatch log group. Extra args go to `aws logs tail`.
#   scripts/logs.sh                       # follow everything from the last 10 minutes
#   scripts/logs.sh --since 1h --filter-pattern '"state="'
#   WAYMARK_LOG_STREAM=app scripts/logs.sh   # only the app stream (also: nginx-access, nginx-error, bootstrap)
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
GROUP="${WAYMARK_LOG_GROUP:-/waymark/app}"
ARGS=(--format short)
[[ -n "${WAYMARK_LOG_STREAM:-}" ]] && ARGS+=(--log-stream-name-prefix "$(stack_output InstanceId)/$WAYMARK_LOG_STREAM")
if [[ $# -eq 0 ]]; then set -- --follow --since 10m; fi
exec aws logs tail "$GROUP" "${ARGS[@]}" "$@"
