#!/usr/bin/env bash
# Mizan Linux deployment setup (Stage 9c, first half).
#
# Idempotent - safe to re-run. Installs system Python + venv tooling,
# creates a venv, installs the daemon's lean dependency set
# (requirements-daemon.txt - no PySide6/GUI deps), creates data/ and
# logs/ directories, creates a dedicated non-root service user, and
# renders the systemd unit templates with this checkout's real absolute
# path. Never touches .env - real credentials are a manual, human step
# (see deploy/README.md), not something an automated script should
# generate or silently proceed without.
#
# Usage: bash deploy/setup.sh
# Assumes a typical fresh Ubuntu VPS: apt/systemd/useradd available, and
# either running as root already or a sudo-capable user. Passwordless
# sudo is NOT assumed - a real VPS commonly prompts for a password,
# unlike the WSL box this was developed against.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_DIR/deploy"
SERVICE_USER="${MIZAN_SERVICE_USER:-mizan}"
VENV_DIR="$REPO_DIR/venv"
# Must match app/automation/control.py's DAEMON_SYSTEMD_UNIT/SYSTEMCTL_PATH -
# this is what /start's `sudo -n systemctl start ...` call actually invokes.
DAEMON_UNIT="mizan-daemon.service"
SYSTEMCTL_BIN="/usr/bin/systemctl"

echo "== Mizan deploy setup =="
echo "Repo dir:     $REPO_DIR"
echo "Service user: $SERVICE_USER"
echo

# --- sudo handling -----------------------------------------------------------
# Root already -> no sudo needed. Otherwise require sudo to exist and let it
# prompt for a password like it normally would - never assume NOPASSWD.
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    if ! command -v sudo >/dev/null 2>&1; then
        echo "ERROR: not running as root, and 'sudo' is not available on this box." >&2
        echo "Run this script as root, or install/configure sudo for this user first." >&2
        exit 1
    fi
    SUDO="sudo"
    echo "Not running as root - using sudo (you may be prompted for a password)."
fi

echo
echo "-- Installing system packages (python3, python3-venv, python3-pip) --"
$SUDO apt-get update -qq
$SUDO apt-get install -y python3 python3-venv python3-pip

PYTHON_BIN="$(command -v python3)"
echo "Using system Python: $PYTHON_BIN ($("$PYTHON_BIN" --version))"

echo
echo "-- Dedicated service user '$SERVICE_USER' (no login shell) --"
if id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "User '$SERVICE_USER' already exists - skipping."
else
    $SUDO useradd --system --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "Created system user '$SERVICE_USER'."
fi

echo
echo "-- venv at $VENV_DIR --"
if [ -x "$VENV_DIR/bin/python" ]; then
    echo "venv already exists - skipping creation."
else
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    echo "Created venv."
fi

echo
echo "-- Installing requirements-daemon.txt --"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -q -r "$REPO_DIR/requirements-daemon.txt"
echo "Installed."

echo
echo "-- Creating data/ and logs/ --"
mkdir -p "$REPO_DIR/data" "$REPO_DIR/logs"

echo
echo "-- Ownership: $SERVICE_USER --"
$SUDO chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR"

echo
echo "-- Rendering systemd unit files with this checkout's path --"
for template in "$DEPLOY_DIR"/*.service.template; do
    unit_name="$(basename "$template" .template)"
    sed \
        -e "s#__REPO_DIR__#$REPO_DIR#g" \
        -e "s#__SERVICE_USER__#$SERVICE_USER#g" \
        "$template" > "$DEPLOY_DIR/$unit_name"
    echo "Rendered $DEPLOY_DIR/$unit_name"
done

echo
echo "-- Provisioning sudoers rule for systemd control of $DAEMON_UNIT --"
# The Telegram bot's /start (app/automation/control.py::launch_daemon(),
# Stage 9c hotfix) asks systemd to start the daemon unit rather than
# spawning daemon.py directly - it runs as "$SERVICE_USER", which needs
# passwordless permission for exactly the three systemctl calls involved.
# Deliberately narrow (never NOPASSWD: ALL) - see deploy/README.md.
SUDOERS_FILE="/etc/sudoers.d/mizan-systemctl"
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
# Managed by deploy/setup.sh - do not edit by hand, re-run the script instead.
# Lets '$SERVICE_USER' start/stop/check exactly $DAEMON_UNIT without a
# password, and nothing else. Needed because app/automation/control.py::
# launch_daemon() asks systemd to start the daemon rather than spawning it
# directly (Stage 9c) - see deploy/README.md.
Cmnd_Alias MIZAN_SYSTEMCTL = $SYSTEMCTL_BIN start $DAEMON_UNIT, $SYSTEMCTL_BIN stop $DAEMON_UNIT, $SYSTEMCTL_BIN status $DAEMON_UNIT
$SERVICE_USER ALL=(root) NOPASSWD: MIZAN_SYSTEMCTL
EOF

if $SUDO visudo -c -f "$SUDOERS_TMP" >/dev/null 2>&1; then
    $SUDO install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_FILE"
    echo "Installed $SUDOERS_FILE"
else
    echo "ERROR: generated sudoers rule failed visudo validation - not installing it." >&2
    echo "Offending content was:" >&2
    cat "$SUDOERS_TMP" >&2
    rm -f "$SUDOERS_TMP"
    exit 1
fi
rm -f "$SUDOERS_TMP"

echo
echo "-- Checking for .env --"
if [ -f "$REPO_DIR/.env" ]; then
    echo "Found $REPO_DIR/.env - good. This script never creates or modifies it."
else
    cat >&2 <<EOF

############################################################
#  .env NOT FOUND at $REPO_DIR/.env
#
#  The daemon and Telegram bot will not run correctly without
#  real credentials. Copy .env.example to .env and fill in
#  ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_PAPER /
#  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID before starting the
#  services. This script deliberately does NOT create it for
#  you - see deploy/README.md.
############################################################

EOF
fi

echo
echo "== Setup complete =="
echo "Next steps (see deploy/README.md for details):"
echo "  1. Make sure $REPO_DIR/.env has real credentials."
echo "  2. sudo cp $DEPLOY_DIR/mizan-daemon.service $DEPLOY_DIR/mizan-telegram-bot.service /etc/systemd/system/"
echo "  3. sudo systemctl daemon-reload"
echo "  4. sudo systemctl enable --now mizan-daemon mizan-telegram-bot"
