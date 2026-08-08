# M17 Phase 3: prod Grafana on a free-tier GCE e2-micro VM

Per `DESIGN_DASHBOARDS.md` §2.4. Mirrors the k3s dev setup
(`deploy/k8s/40-gcs-reader-cronjob.yaml` + `deploy/k8s/50-grafana.yaml`) but
on a standalone Compute Engine VM instead of the Pi cluster, since prod
(`gramunger.com`, Cloud Run) has no cluster to run a bridge CronJob in.

## Architecture

Four pieces run as plain `docker run --restart=always` containers on a
[Container-Optimized OS](https://cloud.google.com/container-optimized-os/docs)
VM (Docker preinstalled, no compose plugin dependency, minimal attack
surface):

- **`caddy`** — the only container with published ports (80/443). Terminates
  TLS for `34-82-149-71.sslip.io` (automatic Let's Encrypt via HTTP-01),
  reverse-proxies to `grafana:3000`, and injects a `Content-Security-Policy:
  frame-ancestors https://gramunger.com` header (replacing whatever Grafana's
  own embedding setting would otherwise omit) so only `gramunger.com` can
  iframe it — not the open internet.
  **Public hostname is `sslip.io`, not `grafana.gramunger.com`:** new
  subdomains on the `gramunger.com` Cloudflare zone don't publish
  (authoritative NXDOMAIN despite the API/dashboard accepting the record —
  the same issue that blocked `stats.`/`analytics.`; root cause is
  account/edge-side and unresolved). `34-82-149-71.sslip.io` resolves to the
  VM's static IP `34.82.149.71` with zero DNS config, sidestepping Cloudflare
  while still getting a real LE cert. The report iframes
  `https://34-82-149-71.sslip.io/d/munger-account-pnl?kiosk`
  (`MUNGER_GRAFANA_URL` on the Cloud Run `daily-screen` Job).
- **`grafana`** — anonymous Viewer-only (mirrors `deploy/k8s/50-grafana.yaml`
  exactly: no login form, no sign-up, Explore disabled, embedding allowed).
  Not published to the host; only reachable through `caddy` over the internal
  Docker network `munger-grafana-net`.
- **`json-server`** — a bare `nginx:alpine` serving the locally-pulled
  `pnl_history.json` **and, as of M17 Phase 2 (2026-08-08), `prices_flat.json`**
  from a bind mount. Not published to the host either — Grafana's Infinity
  datasource (`access: proxy`, i.e. server-side fetch, no browser CORS)
  reaches them at `http://json-server/pnl_history.json` and
  `http://json-server/prices_flat.json` over the same internal network. This
  keeps the JSON data off the public internet entirely, per the design's
  explicit "no public JSON URL" requirement (§2.4) — only the rendered
  Grafana panels are ever externally reachable.
- **A systemd timer** (`munger-gcs-pull.timer`, every 15 min), *not* a
  container — runs a one-shot `google/cloud-sdk:slim` container
  (`--network=host` so it can always reach the GCE metadata server at
  `169.254.169.254`, regardless of the Docker bridge's routing) that pulls
  `pnl_history.jsonl` **and `prices.json`** from `gs://munger-503515-data`
  using the VM's own **instance service account** (`munger-grafana-vm@...`,
  `storage.objectViewer` scoped to the bucket) — no key file anywhere,
  matching the design's "authenticated via GCE metadata" requirement.
  Transforms `pnl_history.jsonl` → a JSON array (Infinity parses JSON, not
  NDJSON) and `prices.json`'s nested-by-symbol shape → a flat
  `{symbol, date, close}` array (same transforms as the k3s reader CronJob /
  `gcs_bridge.py`), writing each pair atomically (temp + `mv`) into the
  `json-server` bind mount. Two dashboards read from these: **Account P&L
  over time** (`pnl_history.json`) and **Held-symbol daily close prices**
  (`prices_flat.json`, M17 Phase 2) — the latter's iframe on
  `gramunger.com/dashboards.html` is gated on `MUNGER_GRAFANA_PRICES_URL`
  being set on the Cloud Run `daily-screen` Job, separately from this VM's
  own setup (see `report.py`'s `_render_dashboards`).

## Rebuild runbook

The VM is provisioned entirely from `bootstrap.sh` (idempotent — fixed
Docker container names, `docker rm -f` before each `docker run`, so a
re-run on a fresh boot converges to the same state). Every other file in
this directory (`Caddyfile`, the `grafana-*` provisioning files,
`pull-pnl-history.sh`) is passed as its own GCE instance-metadata attribute
rather than duplicated inline in `bootstrap.sh` — one source of truth per
file, fetched at boot via the metadata server. To rebuild from scratch:

```bash
gcloud compute instances create munger-grafana \
  --project=munger-503515 --zone=us-west1-b \
  --machine-type=e2-micro \
  --image-family=cos-stable --image-project=cos-cloud \
  --boot-disk-type=pd-standard --boot-disk-size=20GB \
  --service-account=munger-grafana-vm@munger-503515.iam.gserviceaccount.com \
  --scopes=https://www.googleapis.com/auth/devstorage.read_only \
  --address=munger-grafana-ip \
  --tags=munger-grafana \
  --metadata-from-file=startup-script=deploy/gce/bootstrap.sh,grafana-datasources=deploy/gce/grafana-datasources.yaml,grafana-dashboard-provider=deploy/gce/grafana-dashboard-provider.yaml,grafana-dashboard-account-pnl=deploy/gce/grafana-dashboard-account-pnl.json,grafana-dashboard-prices=deploy/gce/grafana-dashboard-prices.json,caddyfile=deploy/gce/Caddyfile,pull-script=deploy/gce/pull-pnl-history.sh
```

To pick up an edit to any of these files on an already-running VM without a
full rebuild: `gcloud compute instances add-metadata munger-grafana
--zone=us-west1-b --metadata-from-file=<key>=<file>`, then re-run
`bootstrap.sh` on the VM (`sudo google_metadata_script_runner startup`) or
just reboot it.

No DNS step is needed: the public hostname is `34-82-149-71.sslip.io`, which
derives from the reserved static IP (`gcloud compute addresses describe
munger-grafana-ip --region=us-west1 --format='value(address)'`) and resolves
without any record. Caddy issues/renews the LE cert automatically. If the
static IP ever changes, the sslip.io hostname changes with it — update the
`Caddyfile` site address and `MUNGER_GRAFANA_URL` accordingly. Rebuild total
time: VM boot + Docker pulls, a few minutes.

## Free-tier conditions (TASKS.md row 510)

- Region `us-west1` (free-tier-eligible), `e2-micro` (the only Compute
  Engine instance anywhere on this billing account — confirmed 2026-07-30).
- Boot disk is `pd-standard` (not SSD), 20 GB — within the 30 GB-month
  Always Free allowance.
- Egress: a Grafana dashboard iframe for a low-traffic personal site is a
  tiny fraction of the 1 GB/month North America free egress (`report-web`'s
  own comparable egress was ~134 MB/month — see TASKS.md row 510).
