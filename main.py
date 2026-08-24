from __future__ import annotations

import argparse
import hashlib
import logging
import signal
import sys
import time
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from config import Config
from report import HourBucket, ReportSnapshot, bucket_windows, build_message, closed_report_window
from state import StateStore
from syabas99 import SyabasClient, SyabasError, Totals
from telegram_bot import TelegramBot, TelegramError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("syabas-report")


class ValidationError(RuntimeError):
    pass

def _slot_key(now: datetime, cfg: Config) -> tuple[str, str]:
    tz = ZoneInfo(cfg.report_timezone)
    report_date, closed_end = closed_report_window(now, tz)
    label = f"{((closed_end.hour + 1) % 24):02d}00"
    return f"{report_date.isoformat()}:{label}", label


def _timeout_handler(signum, frame):
    raise TimeoutError("Job exceeded JOB_TIMEOUT_SECONDS and was stopped so the next watchdog run can retry")


def _same_amount(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= Decimal("0.01")


def collect_snapshot(cfg: Config, client: SyabasClient, now: datetime) -> ReportSnapshot:
    tz = ZoneInfo(cfg.report_timezone)
    report_date, closed_end = closed_report_window(now, tz)
    windows = bucket_windows(report_date, closed_end, tz)
    if not windows:
        raise ValidationError("No completed hour is available yet")

    last_error: Exception | None = None
    for attempt in range(1, cfg.validation_retries + 1):
        buckets: list[HourBucket] = []
        sum_count = 0
        sum_amount = Decimal("0.00")

        for start, end, label in windows:
            totals = client.transaction_totals(start, end)
            buckets.append(HourBucket(start=start, end=end, label=label, totals=totals))
            sum_count += totals.count
            sum_amount += totals.amount

        summed = Totals(count=sum_count, amount=sum_amount.quantize(Decimal("0.01")))

        day_start = datetime.combine(report_date, dtime.min, tzinfo=tz)
        cumulative = client.transaction_totals(day_start, closed_end)
        cumulative_ok = summed.count == cumulative.count and _same_amount(summed.amount, cumulative.amount)

        # The daily report is only a clean apples-to-apples check once the whole day has closed.
        is_final_day = len(windows) == 24
        daily_ok: bool | None = None
        if is_final_day:
            daily = client.daily_report_totals(day_start)
            daily_ok = summed.count == daily.count and _same_amount(summed.amount, daily.amount)

        if cumulative_ok and (not is_final_day or daily_ok or not cfg.strict_final_daily_validation):
            if is_final_day and daily_ok is False:
                log.warning(
                    "Final daily cross-check mismatch; transaction cumulative is internally consistent. "
                    "Hourly=%s/RM%s",
                    summed.count,
                    summed.amount,
                )
            return ReportSnapshot(
                report_date=report_date,
                buckets=buckets,
                totals=cumulative,
                cumulative_verified=True,
                daily_verified=daily_ok,
            )

        last_error = ValidationError(
            "Validation mismatch: "
            f"hourly={summed.count}/RM{summed.amount}, "
            f"cumulative={cumulative.count}/RM{cumulative.amount}, "
            f"daily_ok={daily_ok}"
        )
        if attempt < cfg.validation_retries:
            log.warning("%s; retrying full snapshot in %ss", last_error, cfg.validation_retry_delay_seconds)
            time.sleep(cfg.validation_retry_delay_seconds)
            client.login()  # clean token/session between validation attempts

    raise last_error or ValidationError("Unable to validate report")


def run_once(cfg: Config) -> str:
    state = StateStore(cfg.state_path)
    bot = TelegramBot(cfg, state)
    client = SyabasClient(cfg)

    now = datetime.now(ZoneInfo(cfg.report_timezone))
    slot_key, slot_label = _slot_key(now, cfg)
    log.info("Watchdog tick: Malaysia=%s target_slot=%s", now.strftime("%Y-%m-%d %H:%M:%S"), slot_label)

    # Railway runs this job every 5 minutes. Only the first successful tick for a new
    # report slot does the expensive API work. If a run fails, the slot is NOT marked
    # successful, so the next 5-minute tick retries automatically.
    if not cfg.force_run and not cfg.dry_run and state.get_last_successful_slot() == slot_key:
        log.info("Slot %s already updated successfully; nothing to do", slot_key)
        return "SKIPPED_ALREADY_SUCCESSFUL"

    client.login()
    snapshot = collect_snapshot(cfg, client, now)
    text = build_message(snapshot, cfg)

    if cfg.dry_run:
        print(text)
    else:
        bot.upsert_daily_report(snapshot.report_date.isoformat(), text)
        state.set_last_successful_slot(slot_key)
        bot.clear_error()
        log.info("RUN SUCCESS slot=%s total=%s amount=%s", slot_key, snapshot.totals.count, snapshot.totals.amount)
    return text


def print_chat_ids(cfg: Config) -> None:
    state = StateStore(cfg.state_path)
    bot = TelegramBot(cfg, state)
    updates = bot.get_updates()
    found: dict[str, str] = {}
    for update in updates:
        for key in ("message", "channel_post", "edited_message", "edited_channel_post"):
            msg = update.get(key) or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                name = chat.get("title") or chat.get("username") or chat.get("first_name") or "chat"
                found[str(chat["id"])] = str(name)
    if not found:
        print("No chat IDs found. Send /start to the bot (or a message in the target group), then run again.")
        return
    for chat_id, name in found.items():
        print(f"{chat_id}  {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Syabas99 hourly Telegram deposit report")
    parser.add_argument("--get-chat-id", action="store_true", help="Print chat IDs from Telegram getUpdates")
    parser.add_argument("--test-telegram", action="store_true", help="Send a small Telegram test message")
    args = parser.parse_args()

    cfg = Config.from_env()
    state = StateStore(cfg.state_path)
    bot = TelegramBot(cfg, state)

    # Hard-stop a stuck cron execution before the next 5-minute watchdog tick.
    # Railway skips new cron runs while a previous execution is still Active.
    if cfg.job_timeout_seconds > 0 and hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(cfg.job_timeout_seconds)

    try:
        if args.get_chat_id:
            print_chat_ids(cfg)
            return 0
        if args.test_telegram:
            msg_id = bot.send_message(cfg.telegram_chat_id, "Syabas99 Telegram Bot ✅ Connected", silent=False)
            print(f"Telegram OK, message_id={msg_id}")
            return 0

        run_once(cfg)
        return 0
    except Exception as exc:
        log.exception("Report run failed")
        fingerprint = hashlib.sha256(f"{type(exc).__name__}:{exc}".encode()).hexdigest()[:16]
        try:
            bot.alert_once(
                fingerprint,
                "Syabas99 Deposit Report ERROR\n\n"
                f"{type(exc).__name__}: {str(exc)[:700]}",
            )
        except Exception:
            log.exception("Failed to send Telegram error alert")
        return 1


if __name__ == "__main__":
    sys.exit(main())
