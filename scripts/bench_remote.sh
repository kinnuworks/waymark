#!/usr/bin/env bash
# Run a repo-relative command on the Waymark instance (as user waymark, cwd /opt/waymark)
# and print its output.  Example:
#   scripts/bench_remote.sh 'uv run --project server python -m waymark.bench --frames 200'
# Env: WAYMARK_BENCH_TIMEOUT (seconds, default 1800). Output is capped by SSM at ~24 KB;
# write bigger results to a file and `cat` the summary.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
[[ $# -ge 1 ]] || { echo "usage: $0 '<command run from the repo root>'" >&2; exit 2; }
IID="${WAYMARK_INSTANCE_ID:-$(stack_output InstanceId)}"
TIMEOUT="${WAYMARK_BENCH_TIMEOUT:-1800}"
CMD_B64="$(printf '%s' "$*" | base64 | tr -d '\n')"

REMOTE="set -euo pipefail
T=\$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
echo \"host: \$(hostname)  type: \$(curl -s -m 2 -H \"X-aws-ec2-metadata-token: \$T\" http://169.254.169.254/latest/meta-data/instance-type)  arch: \$(uname -m)  cpus: \$(nproc)\"
cd /opt/waymark
echo \"commit: \$(git log -1 --format='%h %s' 2>/dev/null || echo none)\"
echo \"\$ \$(echo $CMD_B64 | base64 -d)\"
sudo -u waymark -H bash -c 'set -a; . /etc/waymark.env; set +a; cd /opt/waymark; eval \"\$(echo $CMD_B64 | base64 -d)\"'"

ssm_run "$IID" "$TIMEOUT" "$REMOTE"
