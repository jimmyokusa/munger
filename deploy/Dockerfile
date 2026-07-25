# Munger application image (arm64 for the Raspberry Pi k3s cluster).
#
# Runs two things on the cluster, both from this one image:
#   * CI  -- `pytest` as a one-shot Job (deploy/k8s/ci-test-job.yaml)
#   * CD  -- `python daily_screen.py` as a daily CronJob, screen-only,
#            never importing execution.py so it cannot place an order.
#
# python:3.11-slim (Debian bookworm) has prebuilt manylinux aarch64 wheels
# for pandas/numpy/lxml, so no compiler toolchain is needed in the image.
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
