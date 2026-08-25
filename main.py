from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from datetime import datetime, time as dtime
from decimal import Decimal
from zoneinfo import ZoneInfo

from config import Config
from report import HourBucket, ReportSnapshot, bucket_windows, build_message, closed_report_window
from state import StateStore
from syabas99 import SyabasClient, Totals
from telegram_bot import TelegramBot

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

        # Full daily report validation only once 0000 closes the day.
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
            log.warning(
                "%s; retrying full snapshot in %ss",
                last_error,
                cfg.validation_retry_delay_seconds,
            )
            time.sleep(cfg.validation_retry_delay_seconds)
            # Fresh login between complete validation attempts. If another person has
            # logged in, this immediately reclaims the account before retrying.
            client.force_login()

    raise last_error or ValidationError("Unable to validate report")


def run_once(cfg: Config, client: SyabasClient | None = None) -> str:
    state = StateStore(cfg.state_path)
    bot = TelegramBot(cfg, state)
    client = client or SyabasClient(cfg)

    now = datetime.now(ZoneInfo(cfg.report_timezone))
    slot_key, slot_label = _slot_key(now, cfg)
    log.info(
        "Report tick: Malaysia=%s target_slot=%s",
        now.strftime("%Y-%m-%d %H:%M:%S"),
        slot_label,
    )

    if not cfg.force_run and not cfg.dry_run and state.get_last_successful_slot() == slot_key:
        log.info("Slot %s already updated successfully; nothing to do", slot_key)
        return "SKIPPED_ALREADY_SUCCESSFUL"

    # Fresh login before every report cycle. If a human login was the latest session,
    # the report worker immediately takes the session back here.
    client.login(reason="takeover")
    snapshot = collect_snapshot(cfg, client, now)
    text = build_message(snapshot, cfg)

    if cfg.dry_run:
        print(text)
    else:
        bot.upsert_daily_report(snapshot.report_date.isoformat(), text)
        state.set_last_successful_slot(slot_key)
        bot.clear_error()
        log.info(
            "RUN SUCCESS slot=%s total=%s amount=%s",
            slot_key,
            snapshot.totals.count,
            snapshot.totals.amount,
        )
    return text


def _send_error_once(cfg: Config, exc: Exception) -> None:
    state = StateStore(cfg.state_path)
    bot = TelegramBot(cfg, state)
    fingerprint = hashlib.sha256(f"{type(exc).__name__}:{exc}".encode()).hexdigest()[:16]
    try:
        bot.alert_once(
            fingerprint,
            "Syabas99 Deposit Report ERROR\n\n"
            f"{type(exc).__name__}: {str(exc)[:700]}",
        )
    except Exception:
        log.exception("Failed to send Telegram error alert")


def _seconds_into_hour(local_now: datetime) -> int:
    return local_now.minute * 60 + local_now.second


def run_forever(cfg: Config) -> None:
    """
    Always-on Railway worker.

    Why this is more stable than cron for this use case:
    - The process stays alive instead of waiting for the next 5-minute cron run.
    - Every SESSION_GUARD_SECONDS it probes the current authenticated session.
    - If another login invalidates the bot token, the same probe immediately
      force-logins and retries; there is no need to Restart Railway.
    - Every hour, after REPORT_GRACE_SECONDS, it updates the report once.
    - A failed hourly update retries every REPORT_RETRY_SECONDS until it succeeds.
    """
    tz = ZoneInfo(cfg.report_timezone)
    state = StateStore(cfg.state_path)
    client = SyabasClient(cfg)

    log.info(
        "DAEMON START timezone=%s guard=%ss grace=%ss retry=%ss",
        cfg.report_timezone,
        cfg.session_guard_seconds,
        cfg.report_grace_seconds,
        cfg.report_retry_seconds,
    )

    # Claim the account immediately at startup.
    startup_login_ok = False
    try:
        client.login(reason="takeover")
        startup_login_ok = True
    except Exception as exc:
        log.exception("Initial force-login failed; daemon will keep retrying")
        _send_error_once(cfg, exc)

    # Always re-render the currently active daily message once after a deploy/restart.
    # This makes layout-only upgrades visible immediately even if this hour was
    # already marked successful in persistent state.
    if startup_login_ok:
        try:
            now = datetime.now(tz)
            snapshot = collect_snapshot(cfg, client, now)
            text = build_message(snapshot, cfg)
            TelegramBot(cfg, state).upsert_daily_report(snapshot.report_date.isoformat(), text)
            log.info("STARTUP FORMAT REFRESH OK report_date=%s", snapshot.report_date.isoformat())
        except Exception as exc:
            log.exception("Startup format refresh failed; hourly worker will retry normally")
            _send_error_once(cfg, exc)

    next_guard_at = 0.0
    next_report_retry_at = 0.0

    while True:
        loop_now = time.monotonic()
        local_now = datetime.now(tz)

        # Continuous session ownership guard. This is the part that detects a
        # rotated/invalid token caused by another login and takes the session back.
        if cfg.session_guard_enabled and loop_now >= next_guard_at:
            try:
                client.session_healthcheck()
                log.info("SESSION GUARD OK")
            except Exception as exc:
                log.exception("SESSION GUARD failed; forcing login immediately")
                try:
                    client.force_login()
                    client.session_healthcheck()
                    log.warning("SESSION GUARD RECOVERED - account reclaimed immediately")
                except Exception as reclaim_exc:
                    log.exception("SESSION GUARD could not reclaim account yet")
                    _send_error_once(cfg, reclaim_exc)
            next_guard_at = time.monotonic() + cfg.session_guard_seconds

        # Wait a short grace period after the top of the hour so Syabas99 can finish
        # closing the previous hour. After that, retry continuously until success.
        if _seconds_into_hour(local_now) >= cfg.report_grace_seconds:
            slot_key, slot_label = _slot_key(local_now, cfg)
            if state.get_last_successful_slot() != slot_key and loop_now >= next_report_retry_at:
                try:
                    log.info("HOURLY UPDATE DUE slot=%s - running now", slot_label)
                    run_once(cfg, client)
                    next_report_retry_at = 0.0
                except Exception as exc:
                    log.exception(
                        "Hourly update failed; retrying in %ss without waiting for next hour",
                        cfg.report_retry_seconds,
                    )
                    _send_error_once(cfg, exc)
                    next_report_retry_at = time.monotonic() + cfg.report_retry_seconds

        # Short sleep keeps takeover detection responsive without hammering CPU.
        time.sleep(2)


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
    parser.add_argument("--once", action="store_true", help="Run one report cycle and exit")
    args = parser.parse_args()

    cfg = Config.from_env()
    state = StateStore(cfg.state_path)
    bot = TelegramBot(cfg, state)

    try:
        if args.get_chat_id:
            print_chat_ids(cfg)
            return 0
        if args.test_telegram:
            msg_id = bot.send_message(cfg.telegram_chat_id, "Syabas99 Telegram Bot ✅ Connected", silent=False)
            print(f"Telegram OK, message_id={msg_id}")
            return 0
        if args.once or not cfg.run_forever:
            run_once(cfg)
            return 0

        run_forever(cfg)
        return 0
    except KeyboardInterrupt:
        log.info("Daemon stopped")
        return 0
    except Exception as exc:
        log.exception("Report process failed")
        _send_error_once(cfg, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
