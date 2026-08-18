# Deploying Mizan's daemon + Telegram bot to a Linux VPS

This covers the deployment *mechanism* (Stage 9c, first half) - installing and
running the automation daemon and Telegram bot supervisor as systemd services
on a Linux box. It does not cover provisioning the VPS itself (Stage 9c,
second half).

Everything here builds on Stage 9a (confirmed the daemon runs correctly on
Linux) and Stage 9b (the Telegram bot supervisor) - nothing here changes
application behavior, only how the two processes are installed and supervised.

## 1. Clone the repo

```bash
git clone <your-repo-url> mizan
cd mizan
```

(Or `scp`/`rsync` a copy over if you're not using git yet - `deploy/setup.sh`
only cares that the repo files are present at some path; it works out that
path from its own location.)

## 2. Run the setup script

```bash
bash deploy/setup.sh
```

This is safe to re-run at any time. It:

- installs `python3`, `python3-venv`, `python3-pip` via `apt` (prompts for a
  sudo password if you're not root - a fresh VPS is **not** assumed to have
  passwordless sudo, unlike the WSL box this was developed against),
- creates a dedicated, no-login system user (`mizan` by default - override
  with `MIZAN_SERVICE_USER=someuser bash deploy/setup.sh`) so the daemon
  doesn't run as root,
- creates a venv at `venv/` and installs `requirements-daemon.txt` (the lean,
  GUI-free dependency set - no PySide6),
- creates `data/` and `logs/`,
- `chown`s the whole checkout to the service user,
- renders `deploy/mizan-daemon.service` and `deploy/mizan-telegram-bot.service`
  from the `.template` files with this checkout's real absolute path baked in,
- installs a narrowly-scoped sudoers rule at `/etc/sudoers.d/mizan-systemctl`
  (see below),
- checks for `.env` and prints a clear warning (not a silent skip) if it's
  missing.

**It never creates, writes, or touches `.env`.** Real credentials are always a
manual step.

### Why the daemon is systemd-managed, not spawned directly

`/start` (from the GUI or the Telegram bot) doesn't spawn `daemon.py` as a
detached subprocess on Linux the way it does on Windows - it asks **systemd**
to start the already-installed `mizan-daemon.service` unit
(`app/automation/control.py::launch_daemon()`). Spawning `daemon.py` directly
would create a process systemd doesn't know about, running unsupervised
alongside whatever systemd itself might do - a split-brain the daemon's own
single-instance lock can't fully protect against on its own.

Because the Telegram bot supervisor runs as the unprivileged `mizan` user,
this requires `sudo` - so `deploy/setup.sh` provisions a **narrowly scoped**
sudoers rule (`/etc/sudoers.d/mizan-systemctl`, mode `0440`, validated with
`visudo -c -f` before being installed - never a blanket `NOPASSWD: ALL`):

```
Cmnd_Alias MIZAN_SYSTEMCTL = /usr/bin/systemctl start mizan-daemon.service, /usr/bin/systemctl stop mizan-daemon.service, /usr/bin/systemctl status mizan-daemon.service
mizan ALL=(root) NOPASSWD: MIZAN_SYSTEMCTL
```

`launch_daemon()` calls this with `sudo -n` (non-interactive) specifically so
a missing or broken sudoers rule fails immediately with a clear error instead
of hanging on a password prompt that will never arrive - and **there is no
fallback** to spawning `daemon.py` directly if `systemctl` fails. A `/start`
attempt on a misconfigured server replies with a message naming the actual
`sudo`/`systemctl` failure rather than silently doing something unsafe.

## 3. Create `.env`

```bash
cp .env.example .env
nano .env   # or your editor of choice
```

Fill in real values:
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `ALPACA_PAPER=true` - paper-trading
  keys from your Alpaca dashboard.
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` - see the Telegram section of the
  main [README.md](../README.md) for how to get these.

Make sure the file is readable by the service user (`deploy/setup.sh` already
`chown`ed the whole repo, so this is normally already correct - just don't
`chmod` it away from that user afterward).

## 4. Seed the bots (if you haven't already)

```bash
venv/bin/python -m app.automation.setup_bots
```

Idempotent - safe to run again later.

## 5. Install the systemd units

```bash
sudo cp deploy/mizan-daemon.service deploy/mizan-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mizan-daemon mizan-telegram-bot
```

`enable --now` both starts them immediately and registers them to start on
every boot - the systemd equivalent of the Windows Task Scheduler "at system
startup" trigger from Stage 8c.

## Checking status

```bash
systemctl status mizan-daemon
systemctl status mizan-telegram-bot
journalctl -u mizan-daemon -f          # follow live
journalctl -u mizan-daemon -n 50       # last 50 lines
tail -f logs/daemon.log                # the daemon's own structured log (same file either way)
```

`Active: active (running)` plus no repeated restarts in `journalctl` means
it's healthy. A daemon that keeps restarting every ~10s (`RestartSec=10`)
means it's crash-looping - check `journalctl -u mizan-daemon` for the
traceback.

## Stopping - **use the right method for each service**

**The daemon (`mizan-daemon`): use the stop-flag file, not `systemctl stop`.**

```bash
touch data/daemon.stop
```

This is the *same* graceful-shutdown mechanism used everywhere else in this
project (the GUI's Stop button, the Telegram bot's `/stop`, Stage 8b/8c) - the
daemon finishes whatever bot cycle is in flight, logs the shutdown, deletes
the flag file, and exits 0. `systemctl stop mizan-daemon` also works
(`SIGTERM` reaches the same graceful-shutdown code path) - the difference is
that `systemctl stop` imposes a hard ceiling (`TimeoutStopSec=300` in the unit
file) after which systemd force-kills the process if it hasn't exited yet,
whereas `touch data/daemon.stop` has no ceiling at all and always waits for
the current cycle to actually finish. For a long-running trading daemon,
prefer the flag file; `systemctl stop` is a safe fallback within that
5-minute window.

Either way, a graceful stop exits 0, which `Restart=on-failure` does **not**
treat as a failure - it will not be restarted. Contrast with a crash: the
daemon logs and continues past normal errors internally (see
`run_daemon()`'s per-bot exception handling), but something unhandled before
the loop even starts (e.g. the database file becoming unreadable) exits
non-zero, and systemd *will* restart it after `RestartSec=10`.

**The Telegram bot (`mizan-telegram-bot`): `systemctl stop` is correct and the
only option.**

```bash
sudo systemctl stop mizan-telegram-bot
```

It has no flag-file mechanism of its own (Stage 9b didn't build one - there
was no equivalent need, since it isn't mid-trade the way the daemon can be).
`python-telegram-bot`'s polling loop shuts down cleanly on `SIGTERM` well
within the default 90s timeout.

## Uninstalling

```bash
touch data/daemon.stop && sleep 2   # graceful stop first
sudo systemctl disable --now mizan-daemon mizan-telegram-bot
sudo rm /etc/systemd/system/mizan-daemon.service /etc/systemd/system/mizan-telegram-bot.service
sudo rm /etc/sudoers.d/mizan-systemctl
sudo systemctl daemon-reload
```

## Testing the deployment mechanism without a live VPS

`deploy/test/run_docker_test.sh` builds a disposable, systemd-enabled Ubuntu
container from the current repo, drops in a fake test `.env` (fake paper
credentials - confirms the mechanics, not real trading), and runs through the
whole flow above (`setup.sh` → install units → start → stop-flag) inside it:

```bash
bash deploy/test/run_docker_test.sh
```

**Known caveat**: this runs cleanly on a normal Docker host (a real VPS, a CI
runner, plain Linux). On a **WSL2 distro that already has its own systemd
enabled** (`boot.systemd=true` in that distro's `/etc/wsl.conf`), nesting a
second, privileged systemd instance inside a container caused genuine
instability in testing here - the container's `/sbin/init` intermittently
died within seconds, and the WSL kernel log showed
`WaitForBootProcess: /sbin/init failed to start within 10000ms`, i.e. a
conflict between WSL2's own systemd-boot supervision and the nested one, not
a bug in the deploy script or units. If you hit the same thing, the units and
script were instead verified by installing them directly against that WSL
distro's own real systemd (still a disposable-enough Linux box, just without
Docker in the loop) - `deploy/setup.sh` and the unit files don't care whether
they're running in a container or not.
