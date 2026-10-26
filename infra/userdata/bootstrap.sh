#!/bin/bash
# Waymark instance bootstrap (cloud-init user data, runs once as root).
# @@NAME@@ placeholders are substituted by waymark_stack.py at synth time.
set -euxo pipefail
exec > >(tee -a /var/log/waymark-bootstrap.log) 2>&1
export DEBIAN_FRONTEND=noninteractive
APT="apt-get -y -o DPkg::Lock::Timeout=600 -o Acquire::Retries=5"

# ---------------------------------------------------------------- environment
cat > /etc/waymark.env <<'EOF'
WAYMARK_DATA=/var/lib/waymark
WAYMARK_BUCKET=@@BUCKET@@
WAYMARK_TABLE=@@TABLE@@
WAYMARK_INSTANCE=@@INSTANCE_TYPE@@
WAYMARK_REPO_URL=@@REPO_URL@@
AWS_REGION=@@REGION@@
AWS_DEFAULT_REGION=@@REGION@@
HOME=/home/waymark
UV_CACHE_DIR=/home/waymark/.cache/uv
PYTHONUNBUFFERED=1
EOF
chmod 0644 /etc/waymark.env

# ---------------------------------------------------------------- packages
$APT update
$APT install git curl ca-certificates build-essential pkg-config jq unzip nginx rsyslog logrotate
curl -fsSL --retry 5 https://astral.sh/uv/@@UV_VERSION@@/install.sh \
  | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
/usr/local/bin/uv --version

# ---------------------------------------------------------------- user + dirs
id waymark >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/waymark --shell /bin/bash waymark
install -d -o waymark -g waymark /var/lib/waymark /opt/waymark
install -d -o syslog -g adm -m 0755 /var/log/waymark
git config --system --add safe.directory /opt/waymark

# ---------------------------------------------------------------- logging (journal -> rsyslog -> file -> CloudWatch)
cat > /etc/rsyslog.d/30-waymark.conf <<'EOF'
if $programname startswith 'waymark' then /var/log/waymark/app.log
& stop
EOF
cat > /etc/logrotate.d/waymark <<'EOF'
/var/log/waymark/*.log {
  daily
  rotate 3
  compress
  delaycompress
  missingok
  notifempty
  postrotate
    /usr/lib/rsyslog/rsyslog-rotate
  endscript
}
EOF
systemctl restart rsyslog

# ---------------------------------------------------------------- nginx (port 80 -> 127.0.0.1:8000)
cat > /etc/nginx/sites-available/waymark <<'EOF'
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
map $http_cloudfront_forwarded_proto $waymark_proto { default $http_cloudfront_forwarded_proto; '' $scheme; }
server {
  listen 80 default_server;
  listen [::]:80 default_server;
  server_name _;
  client_max_body_size 10m;
  location = /edge-health { access_log off; default_type text/plain; return 200 "ok\n"; }
  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $waymark_proto;
    proxy_connect_timeout 60s;
    proxy_send_timeout 60s;
    proxy_read_timeout 60s;
    proxy_buffering off;
  }
}
EOF
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/waymark /etc/nginx/sites-enabled/waymark
nginx -t
systemctl enable nginx
systemctl restart nginx

# ---------------------------------------------------------------- app service
cat > /etc/systemd/system/waymark.service <<'EOF'
[Unit]
Description=Waymark API (uvicorn)
After=network-online.target
Wants=network-online.target
ConditionPathExists=/opt/waymark/server/pyproject.toml

[Service]
User=waymark
Group=waymark
WorkingDirectory=/opt/waymark
EnvironmentFile=/etc/waymark.env
ExecStart=/usr/local/bin/uv run --project server uvicorn waymark.api:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
TimeoutStartSec=900
SyslogIdentifier=waymark

[Install]
WantedBy=multi-user.target
EOF

# Clone/install loop: retried by a timer until the repo has server/pyproject.toml.
cat > /usr/local/bin/waymark-sync <<'EOF'
#!/bin/bash
set -euo pipefail
. /etc/waymark.env
export GIT_TERMINAL_PROMPT=0
wm() { sudo -u waymark -H env GIT_TERMINAL_PROMPT=0 UV_CACHE_DIR="$UV_CACHE_DIR" "$@"; }
cd /opt/waymark
if [ -d .git ] && ! git rev-parse --verify -q HEAD >/dev/null; then
  echo "waymark-sync: checkout has no commits yet, recloning"
  find /opt/waymark -mindepth 1 -delete
fi
if [ ! -d .git ]; then
  if ! wm git clone --quiet "$WAYMARK_REPO_URL" /opt/waymark; then
    echo "waymark-sync: repo $WAYMARK_REPO_URL not available yet"; exit 0
  fi
else
  wm git pull --ff-only --quiet || echo "waymark-sync: git pull failed (continuing)"
fi
if [ ! -f server/pyproject.toml ]; then
  echo "waymark-sync: waiting for server/pyproject.toml"; exit 0
fi
if [ -f scripts/fetch_models.sh ]; then wm bash scripts/fetch_models.sh; else echo "waymark-sync: WARNING scripts/fetch_models.sh missing"; fi
wm /usr/local/bin/uv sync --project server
systemctl enable waymark.service
systemctl restart waymark.service
touch /var/lib/waymark/.bootstrapped
systemctl disable waymark-sync.timer || true
systemctl stop waymark-sync.timer || true
echo "waymark-sync: installed $(git rev-parse --short HEAD)"
EOF
chmod 0755 /usr/local/bin/waymark-sync

cat > /etc/systemd/system/waymark-sync.service <<'EOF'
[Unit]
Description=Waymark clone/install (retried until the repo is ready)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/waymark-sync
TimeoutStartSec=1800
SyslogIdentifier=waymark-sync
EOF
cat > /etc/systemd/system/waymark-sync.timer <<'EOF'
[Unit]
Description=Retry Waymark clone/install every 2 minutes until it succeeds

[Timer]
OnBootSec=30s
OnUnitInactiveSec=2min

[Install]
WantedBy=timers.target
EOF

# ---------------------------------------------------------------- CloudWatch agent
curl -fsSL --retry 5 -o /tmp/amazon-cloudwatch-agent.deb \
  https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/arm64/latest/amazon-cloudwatch-agent.deb
dpkg -i -E /tmp/amazon-cloudwatch-agent.deb
rm -f /tmp/amazon-cloudwatch-agent.deb
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'EOF'
{
  "agent": { "run_as_user": "root", "region": "@@REGION@@", "metrics_collection_interval": 60 },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          { "file_path": "/var/log/waymark/app.log", "log_group_name": "@@LOG_GROUP@@", "log_stream_name": "{instance_id}/app" },
          { "file_path": "/var/log/nginx/access.log", "log_group_name": "@@LOG_GROUP@@", "log_stream_name": "{instance_id}/nginx-access" },
          { "file_path": "/var/log/nginx/error.log", "log_group_name": "@@LOG_GROUP@@", "log_stream_name": "{instance_id}/nginx-error" },
          { "file_path": "/var/log/waymark-bootstrap.log", "log_group_name": "@@LOG_GROUP@@", "log_stream_name": "{instance_id}/bootstrap" }
        ]
      }
    }
  },
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": { "InstanceId": "${aws:InstanceId}" },
    "metrics_collected": {
      "mem": { "measurement": ["mem_used_percent"] },
      "disk": { "measurement": ["used_percent"], "resources": ["/"], "drop_device": true }
    }
  }
}
EOF
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

# ---------------------------------------------------------------- go
systemctl daemon-reload
systemctl enable --now waymark-sync.timer
systemctl start --no-block waymark-sync.service
echo "waymark bootstrap complete"
