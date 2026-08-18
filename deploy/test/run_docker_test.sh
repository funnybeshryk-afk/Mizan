#!/usr/bin/env bash
# Builds a disposable systemd-enabled Ubuntu container from the current
# repo, runs deploy/setup.sh inside it with fake test credentials, installs
# the real systemd units, and starts both services - a repeatable way to
# smoke-test the deployment mechanism without a live VPS. See
# deploy/README.md for the known WSL2-nested-systemd caveat if this hangs
# or the container exits unexpectedly on a WSL2 host with its own systemd
# enabled - it runs cleanly on a normal Docker host.
#
# Usage: bash deploy/test/run_docker_test.sh
# Requires: docker, with permission to run --privileged containers.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="$(mktemp -d)"
IMAGE_NAME="mizan-systemd-test"
CONTAINER_NAME="mizan-systemd-test"

cleanup() {
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

echo "== Staging build context =="
mkdir -p "$BUILD_DIR/repo"
rsync -a \
    --exclude='venv/' --exclude='venv-linux/' --exclude='dist/' --exclude='build/' \
    --exclude='__pycache__/' --exclude='.pytest_cache/' --exclude='*.pyc' \
    --exclude='data/*.db' \
    "$REPO_ROOT/" "$BUILD_DIR/repo/"
cp "$REPO_ROOT/deploy/test/Dockerfile" "$BUILD_DIR/Dockerfile"

echo "== Building image =="
docker build -t "$IMAGE_NAME" "$BUILD_DIR"

echo "== Starting container (privileged, real systemd as PID 1) =="
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER_NAME" --privileged -v /sys/fs/cgroup:/sys/fs/cgroup:rw "$IMAGE_NAME"
sleep 3
docker exec "$CONTAINER_NAME" systemctl is-system-running || true

echo "== Writing fake test .env (fake paper credentials - mechanics only, not real trading) =="
docker exec "$CONTAINER_NAME" bash -c "cat > /root/mizan/.env" <<'ENVEOF'
ALPACA_API_KEY=PKFAKE1234567890ABCD
ALPACA_SECRET_KEY=fakeSecretKeyForDeploymentTestOnly1234567890
ALPACA_PAPER=true
TELEGRAM_BOT_TOKEN=123456789:FAKE-TOKEN-FOR-DEPLOYMENT-TEST-ONLY
TELEGRAM_CHAT_ID=123456789
ENVEOF

echo "== Running deploy/setup.sh inside the container =="
docker exec "$CONTAINER_NAME" bash /root/mizan/deploy/setup.sh

echo "== Installing and starting the units =="
docker exec "$CONTAINER_NAME" bash -c "
    cp /root/mizan/deploy/mizan-daemon.service /root/mizan/deploy/mizan-telegram-bot.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now mizan-daemon mizan-telegram-bot
"
sleep 5

echo "== Daemon status (expect: active, logging startup) =="
docker exec "$CONTAINER_NAME" systemctl status mizan-daemon --no-pager -l || true
docker exec "$CONTAINER_NAME" cat /root/mizan/logs/daemon.log || true

echo "== Testing graceful stop-flag (expect: clean exit, no restart) =="
docker exec "$CONTAINER_NAME" touch /root/mizan/data/daemon.stop
sleep 3
docker exec "$CONTAINER_NAME" systemctl show mizan-daemon -p ActiveState -p Result -p NRestarts

echo "Done. Container '$CONTAINER_NAME' will be removed on exit (see trap)."
