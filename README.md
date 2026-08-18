# Mizan

A personal investment/trading terminal (Python + PySide6 + SQLAlchemy/SQLite): market data, paper/live broker execution, strategies, backtesting, risk management, and an automation daemon that runs trading bots independently of the GUI.

## Automation daemon: running at Windows startup

The automation daemon (`app/automation/daemon.py`, packaged as `MizanDaemon.exe`) polls a set of paper-trading bots on their own schedules and can run entirely headless - no GUI, no logged-in session required. This section covers installing it as a Windows Scheduled Task so it survives a reboot.

**Paper-trading only**: the daemon can never construct a live broker - see `app/automation/daemon.py::build_bot()`, which hardcodes `PaperBroker`.

### Build

```
pip install -r requirements-build.txt
pyinstaller mizan_daemon.spec
```

Produces `dist/MizanDaemon.exe` - a separate, lean build from `Mizan.exe` (no PySide6). Copy `.env` and (if you have one already) `data/mizan.db` next to `dist/MizanDaemon.exe` - neither is bundled into the executable; both are read at runtime from the directory containing the .exe (`app/core/paths.py::get_base_dir()`).

If you haven't seeded any bots yet:

```
python -m app.automation.setup_bots
```

### Install (run at every startup, logged in or not)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_daemon_task.ps1
```

This registers a Scheduled Task (default name `MizanDaemon`) that:
- triggers **at system startup** (not "at logon" - it must come up with nobody signed in),
- runs as `SYSTEM` so it doesn't need a stored user password,
- restarts automatically on a crash (non-zero exit), up to 3 attempts, 1 minute apart - a clean stop-flag/Ctrl+C shutdown exits 0 and is never restarted,
- has no execution time limit (Task Scheduler's default 72-hour cap would otherwise kill a long-running daemon).

Start it immediately without waiting for a reboot:

```powershell
Start-ScheduledTask -TaskName MizanDaemon
```

### Verify it's running

```powershell
Get-ScheduledTask -TaskName MizanDaemon | Get-ScheduledTaskInfo
Get-Content dist\logs\daemon.log -Wait -Tail 20
```

A live daemon logs each bot's evaluation on its own `poll_interval_seconds` cadence. `LastTaskResult = 0` in `Get-ScheduledTaskInfo` means the most recent run exited cleanly.

### Stop it

The daemon's primary stop mechanism is a flag file, not Ctrl+C (Task Scheduler sessions may have no console to send that to):

```powershell
New-Item -ItemType File -Force dist\data\daemon.stop
```

It finishes whatever bot cycle is in flight, logs the shutdown, deletes the flag file, and exits 0 (so Task Scheduler does not treat this as a failure and does not restart it).

Only one daemon instance can ever run against the same accounts at a time - a lock file (`data/daemon.lock`, holding the PID) makes a second launch (manual or a Task Scheduler restart racing a still-alive process) exit immediately with a logged message instead of running alongside the first.

### Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\unregister_daemon_task.ps1
```

Requests a graceful stop (same flag-file mechanism as above), waits for the process to exit, then removes the scheduled task.

## Telegram bot supervisor (remote notifications and control)

`telegram_bot.py` (entry point at the repo root, logic in `app/automation/telegram_bot_app.py`) is a separate, always-alive process - not a modification to the daemon itself. It gives you:

- **`/status`** - the same status the GUI's "Автоматизация" panel shows: whether the daemon is running (PID + uptime) and a per-bot table (symbol, risk profile, halted state, last signal, last trade).
- **`/stop`** - requests a graceful shutdown via the same `data/daemon.stop` flag file the GUI's Stop button and the daemon's own shutdown logic already use.
- **`/start`** - launches the daemon as a detached process (`MizanDaemon.exe` on Windows, `python daemon.py` on Linux) - the daemon's own single-instance lock (`data/daemon.lock`) is what prevents a duplicate, not the bot.
- **Proactive notifications** - it tails `logs/daemon.log` from the moment it starts (never replaying old history) and forwards a message for: daemon starting/stopping, a rejected duplicate start, a `RISK_STOP_LOSS`/`RISK_TAKE_PROFIT` forced exit, a bot being halted by its circuit breaker, and any `[ERROR]`-level line.

**Every command is checked against `TELEGRAM_CHAT_ID`** - messages from any other chat are silently ignored (logged at debug level only; the bot never confirms or denies its own behavior to an unrecognized sender).

### Setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram, create a bot, copy the token it gives you.
2. Message your new bot once (anything), then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and find `"chat":{"id": ...}` - that's your `TELEGRAM_CHAT_ID`.
3. Copy `.env.example` to `.env` (if you haven't already) and fill in `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`. Never commit `.env`.
4. `pip install -r requirements-daemon.txt` (includes `python-telegram-bot`).

### Run

```
python telegram_bot.py
```

Runs independently of the daemon - `/status` correctly reports "not running" even if the daemon isn't up, and the supervisor doesn't need or take the daemon's lock. For it to survive a reboot on Windows, register it as its own Scheduled Task the same way as `MizanDaemon.exe` (see above), pointing at `python.exe telegram_bot.py`; on Linux, see the next section.

## Linux deployment (systemd)

For running the daemon and Telegram bot on a Linux VPS instead of Windows - a deploy script, two systemd unit templates, and step-by-step install/stop/uninstall instructions: see [deploy/README.md](deploy/README.md).
