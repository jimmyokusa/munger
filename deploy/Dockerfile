# Munger application image: the Cloud Run `daily-screen` job.
#
# Runs `python daily_screen.py` (the CMD below) on a daily Cloud Scheduler
# trigger. Screen-only, never importing execution.py, so it cannot place an
# order. Built by deploy/cloudrun/deploy.sh via Cloud Build. Until
# 2026-10-20 this same image also ran on the retired Pi k3s cluster, as an
# arm64 CronJob plus an in-cluster pytest Job (deploy/archive/k3s/).
#
# python:3.11-slim (Debian bookworm) has prebuilt manylinux wheels for
# pandas/numpy/lxml on both amd64 and arm64, so no compiler toolchain is
# needed in the image.
FROM python:3.11-slim

WORKDIR /app

# Deps first for layer caching; requirements.txt rarely changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Writable runtime artifacts (report/, archives, journal.db, logs) live
# here, backed by a PersistentVolume on the cluster -- see config.py
# DATA_DIR / MUNGER_DATA_DIR.
ENV MUNGER_DATA_DIR=/data

# Run unprivileged; the kubelet chowns the mounted PVC to fsGroup, so the
# container just needs a non-root uid that owns /data by default.
RUN useradd --uid 1000 --create-home munger \
    && mkdir -p /data \
    && chown munger:munger /data
USER munger

CMD ["python", "daily_screen.py"]
