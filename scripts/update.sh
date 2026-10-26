#!/usr/bin/env bash
# Pull the latest code on the instance, re-sync deps and models, restart the service.
# No SSH: runs through SSM Run Command. Push to GitHub first.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
IID="${WAYMARK_INSTANCE_ID:-$(stack_output InstanceId)}"

read -r -d '' REMOTE <<'SH' || true
set -euo pipefail
if [ ! -f /var/lib/waymark/.bootstrapped ]; then
  echo "first install: running waymark-sync"
  /usr/local/bin/waymark-sync
  [ -f /var/lib/waymark/.bootstrapped ] || { echo "repo not ready yet (needs server/pyproject.toml)"; exit 1; }
else
  cd /opt/waymark
  sudo -u waymark -H env GIT_TERMINAL_PROMPT=0 git pull --ff-only
  sudo -u waymark -H bash scripts/fetch_models.sh
  sudo -u waymark -H uv sync --project server
  systemctl restart waymark
fi
echo "commit: $(git -C /opt/waymark log -1 --format='%h %s')"
for i in $(seq 1 30); do
  if curl -fsS -m 5 http://127.0.0.1:8000/api/health; then echo; echo "healthy after ${i}s"; exit 0; fi
  sleep 2
done
echo "service did not become healthy; recent logs:"
systemctl --no-pager status waymark | head -20 || true
journalctl -u waymark --no-pager -n 50 || true
exit 1
SH

ssm_run "$IID" 1200 "$REMOTE"
