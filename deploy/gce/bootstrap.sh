#!/bin/bash
# GCE startup-script for the munger-grafana VM (Container-Optimized OS).
# Runs on every boot; idempotent (fixed container names, `docker rm -f`
# before each `docker run`), so a reboot or a re-run after editing the
# sibling metadata files in this directory converges to the same state.
#
# Each of the other files in deploy/gce/ (Caddyfile, grafana-*.yaml/json,
# pull-pnl-history.sh) is passed as its own GCE instance-metadata
# attribute -- not duplicated inline here -- so there is exactly one
# source of truth per file; this script only fetches and places them.
set -euo pipefail

META="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
fetch() { curl -sf -H "Metadata-Flavor: Google" "$META/$1"; }

mkdir -p /var/lib/munger/data /var/lib/munger/provisioning/datasources \
  /var/lib/munger/provisioning/dashboards-provider /var/lib/munger/provisioning/dashboards \
  /var/lib/munger/caddy /var/lib/munger/scripts

fetch grafana-datasources > /var/lib/munger/provisioning/datasources/datasources.yaml
fetch grafana-dashboard-provider > /var/lib/munger/provisioning/dashboards-provider/provider.yaml
fetch grafana-dashboard-account-pnl > /var/lib/munger/provisioning/dashboards/account-pnl.json
fetch grafana-dashboard-prices > /var/lib/munger/provisioning/dashboards/prices.json
fetch caddyfile > /var/lib/munger/caddy/Caddyfile
fetch pull-script > /var/lib/munger/scripts/pull-pnl-history.sh
chmod +x /var/lib/munger/scripts/pull-pnl-history.sh

docker network inspect munger-grafana-net >/dev/null 2>&1 || \
  docker network create munger-grafana-net

# json-server: internal-only (no -p published ports) -- serves the
# pulled pnl_history.json to Grafana's server-side (access: proxy)
# Infinity fetch over the Docker network. Never reachable from outside
# the VM, per the design's "no public JSON URL" requirement.
docker rm -f json-server >/dev/null 2>&1 || true
docker run -d --name json-server --restart=always \
  --network munger-grafana-net \
  -v /var/lib/munger/data:/usr/share/nginx/html:ro \
  nginx:alpine

# grafana: anonymous Viewer-only, embedding allowed, not published to
# the host -- only reachable through caddy. Mirrors
# deploy/k8s/50-grafana.yaml's container env exactly, including the
# Infinity plugin version pin -- see that file's "INFINITY PLUGIN"
# comment for why (a real, currently-unfixed v3.11.2 regression breaks
# the plugin's frontend entirely). This VM's --restart=always container
# only re-fetches "latest" on a genuine restart (crash, reboot, a
# bootstrap.sh rerun), not continuously, but the exposure is identical:
# whichever version is "latest" at that moment silently replaces
# whatever's currently running.
docker rm -f grafana >/dev/null 2>&1 || true
docker run -d --name grafana --restart=always \
  --network munger-grafana-net \
  -e GF_INSTALL_PLUGINS="yesoreyeram-infinity-datasource 3.11.1" \
  -e GF_AUTH_ANONYMOUS_ENABLED=true \
  -e GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
  -e GF_AUTH_DISABLE_LOGIN_FORM=true \
  -e GF_AUTH_BASIC_ENABLED=false \
  -e GF_USERS_ALLOW_SIGN_UP=false \
  -e GF_USERS_VIEWERS_CAN_EDIT=false \
  -e GF_EXPLORE_ENABLED=false \
  -e GF_NEWS_NEWS_FEED_ENABLED=false \
  -e GF_ANALYTICS_REPORTING_ENABLED=false \
  -e GF_ANALYTICS_CHECK_FOR_UPDATES=false \
  -e GF_SECURITY_ALLOW_EMBEDDING=true \
  -v /var/lib/munger/provisioning/datasources:/etc/grafana/provisioning/datasources:ro \
  -v /var/lib/munger/provisioning/dashboards-provider:/etc/grafana/provisioning/dashboards:ro \
  -v /var/lib/munger/provisioning/dashboards:/var/lib/grafana/dashboards:ro \
  grafana/grafana:11.3.0

# caddy: the only container with published ports. Automatic HTTPS
# (Let's Encrypt HTTP-01) for 34-82-149-71.sslip.io -- the sslip.io hostname
# resolves to this VM's static IP with no DNS record, so the cert issues on
# first boot (see Caddyfile for why sslip.io and not grafana.gramunger.com).
# If issuance can't complete yet, Caddy logs and retries in the background,
# no crash loop. caddy_data is a named volume
# (not the ephemeral container filesystem) so the issued cert survives a
# `docker rm -f`/restart, not just a process restart.
docker volume inspect caddy_data >/dev/null 2>&1 || docker volume create caddy_data
docker rm -f caddy >/dev/null 2>&1 || true
docker run -d --name caddy --restart=always \
  --network munger-grafana-net \
  -p 80:80 -p 443:443 \
  -v /var/lib/munger/caddy/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v caddy_data:/data \
  caddy:2-alpine

# The GCS pull: a systemd timer (not a container) running a one-shot
# google/cloud-sdk:slim container every 15 minutes, --network=host so it
# can always reach the metadata server regardless of the Docker bridge's
# routing (auth is the VM's attached instance service account,
# munger-grafana-vm@..., via metadata -- no key file anywhere here).
cat > /etc/systemd/system/munger-gcs-pull.service <<'UNIT'
[Unit]
Description=Pull pnl_history.jsonl from GCS for the local Grafana dashboard

[Service]
Type=oneshot
ExecStart=/usr/bin/docker run --rm --network=host \
  -v /var/lib/munger/data:/data \
  -v /var/lib/munger/scripts/pull-pnl-history.sh:/pull.sh:ro \
  google/cloud-sdk:slim bash /pull.sh
UNIT

cat > /etc/systemd/system/munger-gcs-pull.timer <<'UNIT'
[Unit]
Description=Run munger-gcs-pull.service every 15 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now munger-gcs-pull.timer
# Prime the data once at boot so json-server isn't empty until the first
# timer fire (systemctl start on a oneshot Service blocks until it exits).
systemctl start munger-gcs-pull.service || true
