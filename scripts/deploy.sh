#!/usr/bin/env bash
# Deploy the Waymark CDK stack. Bootstraps CDK once if needed.
# Writes stack outputs to infra/outputs.json.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$WAYMARK_ROOT/infra"
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1
CDK="npx -y aws-cdk@2"

if [[ ! -x .venv/bin/python ]]; then uv venv -q .venv --python 3.12; fi
uv pip install -q --python .venv/bin/python -r requirements.txt

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if ! aws cloudformation describe-stacks --stack-name CDKToolkit >/dev/null 2>&1; then
  echo ">> bootstrapping CDK in $ACCOUNT/$AWS_REGION"
  $CDK bootstrap "aws://$ACCOUNT/$AWS_REGION"
fi

$CDK deploy "$STACK_NAME" --require-approval never --outputs-file outputs.json "$@"

echo
echo "== $STACK_NAME outputs (infra/outputs.json) =="
jq -r ".[\"$STACK_NAME\"] | to_entries[] | \"\(.key)\t\(.value)\"" outputs.json | column -t -s $'\t'
