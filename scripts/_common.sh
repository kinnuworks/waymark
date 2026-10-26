# Shared helpers for Waymark ops scripts. Source it; do not run it.
# shellcheck shell=bash
set -euo pipefail

WAYMARK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_NAME="${WAYMARK_STACK:-Waymark}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PAGER=""

# stack_output <OutputKey>  -> value from the deployed CloudFormation stack
stack_output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# ssm_run <instance-id> <timeout-seconds> <shell script text>
# Runs the script as root under bash (AWS-RunShellScript itself uses /bin/sh = dash),
# waits, prints stdout/stderr, and succeeds only if the remote script exited 0.
ssm_run() {
  local iid="$1" timeout="$2" script="$3" params cid st
  local b64; b64="$(printf '%s' "$script" | base64 | tr -d '\n')"
  params="$(jq -n --arg c "echo $b64 | base64 -d | bash" --arg t "$timeout" '{commands: [$c], executionTimeout: [$t]}')"
  cid="$(aws ssm send-command --instance-ids "$iid" --document-name AWS-RunShellScript \
    --comment "waymark $(basename "$0")" --timeout-seconds 600 \
    --parameters "$params" --query Command.CommandId --output text)"
  echo ">> SSM command $cid on $iid" >&2
  local deadline=$(( $(date +%s) + timeout + 120 ))
  while :; do
    sleep 3
    st="$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$iid" \
      --query Status --output text 2>/dev/null || echo Pending)"
    case "$st" in
      Pending|InProgress|Delayed) ;;
      *) break ;;
    esac
    if (( $(date +%s) > deadline )); then echo ">> gave up waiting (status=$st)" >&2; return 124; fi
  done
  local out
  out="$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$iid" --output json)"
  echo "$out" | jq -r '.StandardOutputContent'
  local err; err="$(echo "$out" | jq -r '.StandardErrorContent')"
  [[ -n "$err" ]] && { echo "---- stderr ----" >&2; echo "$err" >&2; }
  local rc; rc="$(echo "$out" | jq -r '.ResponseCode')"
  echo ">> status=$st exit=$rc" >&2
  [[ "$st" == "Success" ]]
}
