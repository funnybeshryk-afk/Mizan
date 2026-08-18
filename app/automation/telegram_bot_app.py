from __future__ import annotations

import logging
import os
import warnings
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.warnings import PTBUserWarning

from app.automation import control
from app.automation.log_notifier import tail_log_and_notify
from app.core.formatting import format_uptime
from app.core.paths import BASE_DIR, DB_PATH, ENV_PATH, LOGS_DIR
from app.database.session import get_engine, get_session_factory

logger = logging.getLogger("mizan.telegram_bot")

LOG_FILE_NAME = "daemon.log"

# post_init() (below) runs during Application.initialize(), which is always
# before Application.start() flips the app to "running" - so
# create_task()'d there will genuinely run (asyncio schedules it
# regardless), but PTB warns it won't be awaited during a graceful
# Application.stop(). That's fine for this specific task: it's an
# infinite tail -f loop with no state that needs a clean async teardown -
# the process exiting (Ctrl+C/SIGTERM/service stop) is all the cleanup it
# needs, same as the daemon's own log-watching would get from the OS.
warnings.filterwarnings(
    "ignore", message=r"Tasks created via `Application\.create_task`.*", category=PTBUserWarning
)


class ConfigError(Exception):
    """TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing or malformed in .env."""


def load_config() -> tuple[str, int]:
    load_dotenv(ENV_PATH)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not raw_chat_id:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set in .env "
            "(see .env.example) - the bot supervisor refuses to start without them, "
            "since an unset chat id would make the authorization check meaningless."
        )
    try:
        chat_id = int(raw_chat_id)
    except ValueError as exc:
        raise ConfigError(f"TELEGRAM_CHAT_ID must be an integer, got {raw_chat_id!r}") from exc
    return token, chat_id


def is_authorized(update: Update, allowed_chat_id: int) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id == allowed_chat_id


async def _reject_if_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Every handler calls this first. Returns True (and does nothing
    else - no reply, nothing that would confirm or deny the bot's
    behavior to the sender) if the update should be dropped.
    """
    allowed_chat_id = context.bot_data["allowed_chat_id"]
    if is_authorized(update, allowed_chat_id):
        return False

    chat = update.effective_chat
    logger.debug(
        "Ignoring update from unauthorized chat_id=%s (expected %s)",
        chat.id if chat else None,
        allowed_chat_id,
    )
    return True


def build_status_message() -> str:
    """The same status app.ui.automation_panel.AutomationPanel shows in
    the GUI, reused (not reimplemented) via
    app.automation.control.get_daemon_status()/bot_status_rows(), just
    formatted as Telegram text instead of Qt widgets.
    """
    status = control.get_daemon_status()
    if status.running:
        uptime_text = ""
        if status.started_at is not None:
            uptime_text = f", в работе {format_uptime(datetime.now(timezone.utc) - status.started_at)}"
        header = f"🟢 Демон работает (PID {status.pid}{uptime_text})"
    else:
        header = "🔴 Демон остановлен"

    session = get_session_factory(get_engine())()
    try:
        rows = control.bot_status_rows(session)
    finally:
        session.close()

    lines = [header, ""]
    if not rows:
        lines.append("Боты не настроены.")
    for row in rows:
        bot_status = f"Halted: {row.halted_reason}" if row.halted else "Активен"
        signal_text = "—"
        if row.last_signal_at is not None:
            signal_text = f"{row.last_signal_at:%Y-%m-%d %H:%M:%S} ({row.last_signal_action})"
        trade_text = "—"
        if row.last_trade_at is not None:
            trade_text = f"{row.last_trade_at:%Y-%m-%d %H:%M:%S} {row.last_trade_summary}"

        lines.append(
            f"• {row.name} ({row.symbol}, {row.risk_profile_name}) — {bot_status}\n"
            f"  Сигнал: {signal_text}\n"
            f"  Сделка: {trade_text}"
        )
    return "\n".join(lines)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update, context):
        return
    logger.info("Received /status")
    await update.message.reply_text(build_status_message())


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update, context):
        return
    logger.info("Received /stop - requesting daemon shutdown")
    control.request_stop()
    await update.message.reply_text(
        "Запрошена остановка демона (создан data/daemon.stop). "
        "Он завершит текущий цикл и остановится."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update, context):
        return

    logger.info("Received /start")
    exe_path = control.resolve_daemon_exe_path()
    if not exe_path.exists():
        logger.warning("Cannot start daemon - %s does not exist", exe_path)
        await update.message.reply_text(f"Не найден {exe_path} — демон не запущен.")
        return

    try:
        control.launch_daemon(exe_path)
    except control.DaemonLaunchError as exc:
        # No silent fallback: on POSIX this means `sudo -n systemctl start`
        # itself failed (bad sudoers rule, unit not installed, ...) - a
        # misconfigured server must fail loudly here, not silently spawn
        # an unmanaged daemon process outside systemd's supervision.
        logger.error("Failed to launch daemon via systemd: %s", exc)
        await update.message.reply_text(
            f"Не удалось запустить демон через systemd: {exc}. Проверьте sudoers-конфигурацию."
        )
        return

    logger.info("Launched daemon via %s", exe_path)
    await update.message.reply_text("Демон запускается…")


def build_application(token: str, chat_id: int) -> Application:
    application = Application.builder().token(token).post_init(_post_init).build()
    application.bot_data["allowed_chat_id"] = chat_id

    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("start", start_command))
    return application


async def _post_init(application: Application) -> None:
    chat_id = application.bot_data["allowed_chat_id"]
    logger.info("Telegram bot supervisor ready - watching %s for events", LOGS_DIR / LOG_FILE_NAME)
    application.create_task(tail_log_and_notify(application.bot, chat_id, LOGS_DIR / LOG_FILE_NAME))


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # httpx (python-telegram-bot's HTTP client) logs the full request URL at
    # INFO level, and the Telegram Bot API embeds the bot token directly in
    # that URL (https://api.telegram.org/bot<TOKEN>/...) - left at the
    # basicConfig level above, every single API call would print the live
    # token in plaintext to the console/journal. WARNING keeps real
    # connection failures visible without ever logging a successful
    # request's URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    logger.info(
        "Paths: BASE_DIR=%s .env=%s (exists=%s) db=%s (exists=%s)",
        BASE_DIR,
        ENV_PATH,
        ENV_PATH.exists(),
        DB_PATH,
        DB_PATH.exists(),
    )
    token, chat_id = load_config()
    application = build_application(token, chat_id)
    # drop_pending_updates: don't replay commands that arrived while the
    # supervisor was offline - same "don't replay old history" principle
    # as tail_log_and_notify()'s end-of-file seek.
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
