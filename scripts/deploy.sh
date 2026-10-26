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

# Origin secret (X-Waymark-Origin): SecureString in SSM, created once here because
# CloudFormation cannot create SecureString parameters. Never printed.
SECRET_PARAM=/waymark/origin-secret
if ! aws ssm get-parameter --name "$SECRET_PARAM" >/dev/null 2>&1; then
  echo ">> creating SecureString $SECRET_PARAM"
  aws ssm put-parameter --name "$SECRET_PARAM" --type SecureString --tier Standard \
    --description "Waymark origin header secret (API Gateway/CloudFront -> nginx)" \
    --value "$(openssl rand -hex 16)" >/dev/null
fi
ORIGIN_SECRET="$(aws ssm get-parameter --name "$SECRET_PARAM" --with-decryption --query Parameter.Value --output text)"

$CDK deploy "$STACK_NAME" --require-approval never --outputs-file outputs.json \
  --parameters "OriginSecret=$ORIGIN_SECRET" "$@"
unset ORIGIN_SECRET

echo
echo "== $STACK_NAME outputs (infra/outputs.json) =="
jq -r ".[\"$STACK_NAME\"] | to_entries[] | \"\(.key)\t\(.value)\"" outputs.json | column -t -s $'\t'
