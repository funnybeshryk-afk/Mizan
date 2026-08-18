from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.daemon import stop_file_path
from app.automation.lock import read_daemon_status
from app.core.formatting import format_quantity
from app.core.paths import BASE_DIR
from app.database.models import BotConfig, Instrument
from app.database.models import Signal as SignalRecord
from app.database.models import Trade

DAEMON_EXE_NAME = "MizanDaemon.exe"
DAEMON_SCRIPT_NAME = "daemon.py"
DAEMON_SYSTEMD_UNIT = "mizan-daemon.service"
SYSTEMCTL_PATH = "/usr/bin/systemctl"


class DaemonLaunchError(Exception):
    """launch_daemon() failed on POSIX - systemctl refused or couldn't
    start the unit (bad/missing sudoers rule, unit not installed, sudo
    itself missing, ...). Deliberately never caught-and-ignored here:
    falling back to spawning daemon.py directly on failure would recreate
    exactly the split-brain problem this fix exists to prevent (a daemon
    process systemd doesn't know about, running unsupervised alongside
    whatever systemd itself is or isn't doing). Callers must surface this
    to a human - see app.automation.telegram_bot_app.start_command.
    """


@dataclass(frozen=True)
class DaemonStatus:
    """What the GUI's "Автоматизация" panel needs to render - derived
    entirely from app.automation.lock.read_daemon_status(), never a
    second, independent liveness check."""

    running: bool
    pid: int | None = None
    started_at: datetime | None = None


def resolve_daemon_exe_path(base_dir: Path | None = None) -> Path:
    """The thing to launch to start the daemon, resolved next to this
    process's own location - same directory convention as the existing
    Mizan.exe/MizanDaemon.exe pair on Windows (both land in dist/ from a
    real build). In dev mode (`python main.py`), base_dir is the repo
    root, where only daemon.py exists - callers must handle a missing
    file here rather than assume it always exists.

    Extended for Stage 9b (the cross-platform Telegram supervisor, which
    has no .exe to look for at all): on Windows this is still
    MizanDaemon.exe; on POSIX (Linux/systemd, Stage 9a) there is no
    single executable, so this resolves to the daemon.py script itself -
    launch_daemon() below knows how to run each correctly. One function,
    one "what do I launch" answer per platform, not a parallel path.
    """
    base_dir = base_dir or BASE_DIR
    if sys.platform == "win32":
        return base_dir / DAEMON_EXE_NAME
    return base_dir / DAEMON_SCRIPT_NAME


def daemon_exe_exists(base_dir: Path | None = None) -> bool:
    return resolve_daemon_exe_path(base_dir).exists()


def get_daemon_status() -> DaemonStatus:
    info = read_daemon_status()
    if info is None:
        return DaemonStatus(running=False)

    started_at = None
    raw_started_at = info.get("started_at")
    if raw_started_at:
        try:
            started_at = datetime.fromisoformat(raw_started_at)
        except ValueError:
            started_at = None

    return DaemonStatus(running=True, pid=info.get("pid"), started_at=started_at)


def launch_daemon(exe_path: Path) -> subprocess.Popen | None:
    """Starts the daemon, appropriately for the platform. Returns the
    Popen handle on Windows (see below); returns None on POSIX (there is
    no live handle to return - systemd owns the process from here on).

    On Windows: launched fully detached from the calling process (the
    GUI) via DETACHED_PROCESS (no console) + CREATE_NEW_PROCESS_GROUP (its
    own process group, so console events aimed at the caller - e.g.
    Ctrl+C in whatever launched it - never reach the daemon), running
    exe_path directly as MizanDaemon.exe.

    On POSIX (Stage 9c - a VPS hotfix ported back here, see deploy/
    README.md): the daemon runs under systemd (deploy/mizan-daemon.
    service), so "launching" it means asking systemd to start the
    already-installed unit - `sudo -n systemctl start
    mizan-daemon.service` - never spawning daemon.py directly. Spawning
    it directly would create a process systemd doesn't know about,
    running unsupervised alongside (not instead of) the daemon's own
    single-instance lock and whatever systemd itself might do - the exact
    split-brain this fix exists to prevent. `-n` (non-interactive) makes
    a missing/misconfigured sudoers rule fail immediately instead of
    hanging on a password prompt that will never come; the narrowly
    scoped rule this requires is provisioned by deploy/setup.sh (never a
    blanket NOPASSWD: ALL) - see deploy/README.md.

    Raises DaemonLaunchError if systemctl fails for any reason - there is
    deliberately no fallback to a raw subprocess spawn. A misconfigured
    server should fail loudly (surfaced through the Telegram /start
    reply), not silently start an unmanaged daemon.

    Neither platform checks for an existing instance first - that's the
    daemon's own single-instance lock's job (app.automation.lock, Stage
    8c). If a race happens (e.g. a double launch before the status
    catches up), the new instance just refuses to start; this function
    doesn't need to know or care.
    """
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            [str(exe_path)],
            cwd=str(exe_path.parent),
            creationflags=creationflags,
            close_fds=True,
        )

    try:
        result = subprocess.run(
            ["sudo", "-n", SYSTEMCTL_PATH, "start", DAEMON_SYSTEMD_UNIT],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise DaemonLaunchError(str(exc)) from exc

    if result.returncode != 0:
        reason = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"systemctl exited with code {result.returncode}"
        )
        raise DaemonLaunchError(reason)

    return None


def request_stop() -> None:
    """Creates the data/daemon.stop flag file - the same graceful-shutdown
    mechanism the daemon itself already implements (Stage 8b/8c). Not a
    new or different stop path.
    """
    path = stop_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


@dataclass(frozen=True)
class BotStatusRow:
    name: str
    symbol: str
    risk_profile_name: str
    halted: bool
    halted_reason: str | None
    last_signal_at: datetime | None
    last_signal_action: str | None
    last_trade_at: datetime | None
    last_trade_summary: str | None


def bot_status_rows(session: Session) -> list[BotStatusRow]:
    """One row per BotConfig, for the panel's table. Read-only - never
    constructs a Bot/Strategy/Broker like the daemon does, just reports
    what's already in the database.
    """
    configs = list(session.execute(select(BotConfig).order_by(BotConfig.name)).scalars())
    rows = []
    for config in configs:
        last_signal = session.execute(
            select(SignalRecord)
            .join(Instrument, SignalRecord.instrument_id == Instrument.id)
            .where(Instrument.symbol == config.symbol)
            .order_by(SignalRecord.generated_at.desc(), SignalRecord.id.desc())
            .limit(1)
        ).scalar_one_or_none()

        last_trade = session.execute(
            select(Trade)
            .where(Trade.account_id == config.account_id)
            .order_by(Trade.executed_at.desc(), Trade.id.desc())
            .limit(1)
        ).scalar_one_or_none()

        last_trade_summary = None
        if last_trade is not None:
            last_trade_summary = (
                f"{last_trade.side.value} {format_quantity(last_trade.quantity)} @ {last_trade.price:.2f}"
            )

        rows.append(
            BotStatusRow(
                name=config.name,
                symbol=config.symbol,
                risk_profile_name=config.risk_profile_name,
                halted=config.halted,
                halted_reason=config.halted_reason,
                last_signal_at=last_signal.generated_at if last_signal is not None else None,
                last_signal_action=last_signal.action.value if last_signal is not None else None,
                last_trade_at=last_trade.executed_at if last_trade is not None else None,
                last_trade_summary=last_trade_summary,
            )
        )
    return rows
