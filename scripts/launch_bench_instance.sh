#!/usr/bin/env bash
# Launch a temporary, SSM-managed benchmark instance (no inbound ports, no SSH key),
# clone the repo + fetch models on it, run one command from the repo root, print the
# output, and ALWAYS terminate the instance (trap on EXIT/INT/TERM, plus an on-box
# self-destruct timer as a second safety net).
#
# usage: scripts/launch_bench_instance.sh <ami-id|cool|ubuntu-arm64|ubuntu-amd64> <instance-type> ['<command>']
#   scripts/launch_bench_instance.sh cool c8g.large 'bash scripts/bench.sh'
#   scripts/launch_bench_instance.sh ubuntu-amd64 c7i.large 'bash scripts/bench.sh'
#   scripts/launch_bench_instance.sh ami-0123456789abcdef0 c8g.large 'nproc; lscpu'
#
# Env:
#   WAYMARK_REPO_URL       repo to clone (default https://github.com/kinnuworks/waymark.git)
#   WAYMARK_REF            branch/tag/commit to check out (default: repo default branch)
#   WAYMARK_BENCH_TIMEOUT  command timeout in seconds (default 3600)
#   WAYMARK_BENCH_MAX_MIN  on-box self-destruct after N minutes (default 120)
#   WAYMARK_ROOT_GB        root volume size, at least the AMI snapshot size (default 30)
#
# Reuses the Waymark stack's instance profile and the no-inbound BenchSg security group,
# so the stack must be deployed first. The command runs as root in /opt/waymark with
# /etc/waymark-bench.env exported (WAYMARK_INSTANCE, AWS_REGION, WAYMARK_BUCKET, WAYMARK_TABLE, IS_COOL).
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

[[ $# -ge 2 ]] || { sed -n '2,12p' "$0" >&2; exit 2; }
AMI_ARG="$1"; ITYPE="$2"; shift 2
CMD="${*:-uname -a; nproc; lscpu | head -20; python3 --version}"
REPO_URL="${WAYMARK_REPO_URL:-https://github.com/kinnuworks/waymark.git}"
REF="${WAYMARK_REF:-}"
TIMEOUT="${WAYMARK_BENCH_TIMEOUT:-3600}"
MAX_MIN="${WAYMARK_BENCH_MAX_MIN:-120}"
ROOT_GB="${WAYMARK_ROOT_GB:-30}"
COOL_PRODUCT_CODE="aajkmdd4qo3r7yhqg61a7aah9"   # Marketplace prodview-fdvbfiewzuehs (COOL for Graviton4)

# ------------------------------------------------------------------ resolve AMI
IS_COOL=0
case "$AMI_ARG" in
  cool)
    IS_COOL=1
    AMI="$(aws ec2 describe-images --owners aws-marketplace \
      --filters "Name=product-code,Values=$COOL_PRODUCT_CODE" "Name=state,Values=available" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
    [[ "$AMI" == ami-* ]] || { echo "no COOL AMI found for product code $COOL_PRODUCT_CODE" >&2; exit 1; } ;;
  ubuntu-arm64|ubuntu-amd64)
    AMI="$(aws ssm get-parameter --name "/aws/service/canonical/ubuntu/server/24.04/stable/current/${AMI_ARG#ubuntu-}/hvm/ebs-gp3/ami-id" \
      --query Parameter.Value --output text)" ;;
  ami-*) AMI="$AMI_ARG" ;;
  *) echo "unknown AMI '$AMI_ARG'" >&2; exit 2 ;;
esac
read -r AMI_NAME ROOT_DEV SNAP_GB ARCH < <(aws ec2 describe-images --image-ids "$AMI" \
  --query 'Images[0].[Name,RootDeviceName,BlockDeviceMappings[0].Ebs.VolumeSize,Architecture]' --output text)
grep -q "$COOL_PRODUCT_CODE" <<<"$(aws ec2 describe-images --image-ids "$AMI" --query 'Images[0].ProductCodes[].ProductCodeId' --output text)" && IS_COOL=1
(( ROOT_GB < SNAP_GB )) && ROOT_GB="$SNAP_GB"
echo ">> AMI $AMI ($AMI_NAME, $ARCH, root $ROOT_DEV ${ROOT_GB}GB)  type $ITYPE  cool=$IS_COOL" >&2

# ------------------------------------------------------------------ stack resources
PROFILE="$(stack_output InstanceProfileName)"
SG="$(stack_output BenchSecurityGroupId)"
SUBNET="${WAYMARK_BENCH_SUBNET:-$(stack_output BenchSubnetId)}"
BUCKET="$(stack_output BucketName)"
TABLE="$(stack_output TableName)"

# ------------------------------------------------------------------ user data: ensure SSM agent + self-destruct
USER_DATA="$(cat <<EOF
#!/bin/bash
shutdown -h +$MAX_MIN "waymark bench self-destruct" || true
if ! systemctl is-active --quiet snap.amazon-ssm-agent.amazon-ssm-agent && ! systemctl is-active --quiet amazon-ssm-agent; then
  R=$AWS_REGION
  case "\$(uname -m)" in aarch64) D=arm64;; *) D=amd64;; esac
  if command -v dpkg >/dev/null; then
    curl -fsSL --retry 5 -o /tmp/ssm.deb https://s3.\$R.amazonaws.com/amazon-ssm-\$R/latest/debian_\$D/amazon-ssm-agent.deb && dpkg -i /tmp/ssm.deb
  else
    dnf install -y https://s3.\$R.amazonaws.com/amazon-ssm-\$R/latest/linux_\$D/amazon-ssm-agent.rpm || yum install -y https://s3.\$R.amazonaws.com/amazon-ssm-\$R/latest/linux_\$D/amazon-ssm-agent.rpm
  fi
  systemctl enable --now amazon-ssm-agent
fi
EOF
)"

# ------------------------------------------------------------------ launch (+ guaranteed cleanup)
IID=""
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ -n "$IID" ]]; then
    echo ">> terminating $IID" >&2
    aws ec2 terminate-instances --instance-ids "$IID" --query 'TerminatingInstances[0].CurrentState.Name' --output text >&2 || \
      echo "!! terminate failed: run  aws ec2 terminate-instances --instance-ids $IID" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

IID="$(aws ec2 run-instances \
  --image-id "$AMI" --instance-type "$ITYPE" --count 1 \
  --subnet-id "$SUBNET" --security-group-ids "$SG" \
  --iam-instance-profile "Name=$PROFILE" \
  --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
  --instance-initiated-shutdown-behavior terminate \
  --block-device-mappings "[{\"DeviceName\":\"$ROOT_DEV\",\"Ebs\":{\"VolumeSize\":$ROOT_GB,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
  --user-data "$USER_DATA" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=waymark-bench-$ITYPE},{Key=project,Value=waymark},{Key=purpose,Value=bench}]" \
  --query 'Instances[0].InstanceId' --output text)"
echo ">> launched $IID; waiting for running + SSM online" >&2
aws ec2 wait instance-running --instance-ids "$IID"

for i in $(seq 1 120); do
  ping="$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$IID" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)"
  [[ "$ping" == "Online" ]] && break
  (( i == 120 )) && { echo "!! instance never came online in SSM" >&2; exit 1; }
  sleep 5
done
echo ">> SSM online" >&2

CMD_B64="$(printf '%s' "$CMD" | base64 | tr -d '\n')"
REMOTE="set -eu
export HOME=/root DEBIAN_FRONTEND=noninteractive GIT_TERMINAL_PROMPT=0
cloud-init status --wait >/dev/null 2>&1 || true
T=\$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
ITYPE=\$(curl -s -m 2 -H \"X-aws-ec2-metadata-token: \$T\" http://169.254.169.254/latest/meta-data/instance-type)
echo \"== \$(hostname) \$ITYPE \$(uname -m) cpus=\$(nproc) cool=$IS_COOL ami=$AMI\"
if ! command -v git >/dev/null || ! command -v curl >/dev/null; then
  apt-get -y -o DPkg::Lock::Timeout=600 update >/dev/null && apt-get -y -o DPkg::Lock::Timeout=600 install git curl ca-certificates build-essential >/dev/null
fi
command -v uv >/dev/null || curl -fsSL https://astral.sh/uv/0.12.9/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
export PATH=/usr/local/bin:\$PATH
cat > /etc/waymark-bench.env <<ENV
WAYMARK_INSTANCE=\$ITYPE
WAYMARK_DATA=/var/lib/waymark
WAYMARK_BUCKET=$BUCKET
WAYMARK_TABLE=$TABLE
AWS_REGION=$AWS_REGION
IS_COOL=$IS_COOL
ENV
mkdir -p /var/lib/waymark
rm -rf /opt/waymark && git clone --quiet $REPO_URL /opt/waymark
cd /opt/waymark
if [ -n '$REF' ]; then git checkout --quiet '$REF'; fi
echo \"commit: \$(git log -1 --format='%h %s')\"
if [ -f scripts/fetch_models.sh ]; then bash scripts/fetch_models.sh; fi
set -a; . /etc/waymark-bench.env; set +a
echo \"\\\$ \$(echo $CMD_B64 | base64 -d)\"
eval \"\$(echo $CMD_B64 | base64 -d)\""

ssm_run "$IID" "$TIMEOUT" "$REMOTE"
