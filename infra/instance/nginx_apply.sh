#!/bin/bash
# Repo-independent nginx front config for the Waymark instance.
# Delivered by the stack's SSM State Manager association (runs on every deploy that changes
# it, and on replacement instances), installed as /usr/local/bin/waymark-nginx-apply, and
# re-run by scripts/update.sh. Never prints the secret.
#
# Only requests carrying X-Waymark-Origin: <secret> (added by API Gateway / CloudFront) are
# proxied to the app; anything else gets 403. /edge-health answers only to localhost.
set -euo pipefail
REGION="@@REGION@@"
PARAM="@@SECRET_PARAM@@"

# Let first-boot cloud-init finish so its nginx config cannot overwrite this one.
cloud-init status --wait >/dev/null 2>&1 || true

AWS=/snap/bin/aws
if [ ! -x "$AWS" ]; then
  if command -v aws >/dev/null 2>&1; then AWS="$(command -v aws)"; else snap install aws-cli --classic >/dev/null; fi
fi
SECRET="$("$AWS" ssm get-parameter --region "$REGION" --name "$PARAM" --with-decryption \
  --query Parameter.Value --output text)"
if ! [[ "$SECRET" =~ ^[A-Za-z0-9]{32,}$ ]]; then
  echo "waymark-nginx-apply: secret $PARAM missing or malformed" >&2; exit 1
fi

umask 077
cat > /etc/nginx/waymark-origin-secret.conf.new <<EOF
map \$http_x_waymark_origin \$waymark_origin_ok { default 0; "$SECRET" 1; }
EOF
mv -f /etc/nginx/waymark-origin-secret.conf.new /etc/nginx/waymark-origin-secret.conf
umask 022

cat > /etc/nginx/sites-available/waymark <<'EOF'
# Managed by waymark-nginx-apply (Waymark stack SSM association). Local edits are overwritten.
include /etc/nginx/waymark-origin-secret.conf;
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
server {
  listen 80 default_server;
  listen [::]:80 default_server;
  server_name _;
  client_max_body_size 10m;

  location = /edge-health {
    # `return` runs before allow/deny (rewrite vs access phase), so check the address explicitly.
    if ($remote_addr !~ "^(127\.0\.0\.1|::1)$") { return 403; }
    access_log off;
    default_type text/plain;
    return 200 "ok\n";
  }

  location / {
    if ($waymark_origin_ok = 0) { return 403; }
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Waymark-Origin "";
    proxy_connect_timeout 60s;
    proxy_send_timeout 60s;
    proxy_read_timeout 60s;
    proxy_buffering off;
  }
}
EOF
ln -sf /etc/nginx/sites-available/waymark /etc/nginx/sites-enabled/waymark
rm -f /etc/nginx/sites-enabled/default
nginx -t 2>&1 | grep -v "^nginx: \[warn\]" || true
nginx -t >/dev/null 2>&1
systemctl reload nginx || systemctl restart nginx
echo "waymark-nginx-apply: origin header check active"
