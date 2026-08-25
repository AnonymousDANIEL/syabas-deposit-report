from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from config import Config
from syabas99 import Totals


@dataclass(frozen=True)
class HourBucket:
    start: datetime
    end: datetime
    label: str
    totals: Totals


@dataclass(frozen=True)
class ReportSnapshot:
    report_date: date
    buckets: list[HourBucket]
    totals: Totals
    cumulative_verified: bool
    daily_verified: bool | None


def closed_report_window(now: datetime, tz: ZoneInfo) -> tuple[date, datetime]:
    """
    At XX:05 we report the hour that just closed.
    Example: 21:05 -> completed through 20:59:59, label 2100.
    At 00:05 -> completed through previous day 23:59:59, label 0000.
    """
    local_now = now.astimezone(tz)
    current_hour = local_now.replace(minute=0, second=0, microsecond=0)
    closed_end = current_hour - timedelta(microseconds=1)
    return closed_end.date(), closed_end


def bucket_windows(report_date: date, closed_end: datetime, tz: ZoneInfo) -> list[tuple[datetime, datetime, str]]:
    day_start = datetime.combine(report_date, time.min, tzinfo=tz)
    if closed_end < day_start:
        return []

    windows: list[tuple[datetime, datetime, str]] = []
    cursor = day_start
    while cursor <= closed_end:
        end = cursor + timedelta(hours=1) - timedelta(seconds=1)
        label_hour = (cursor.hour + 1) % 24
        label = f"{label_hour:02d}00"
        windows.append((cursor, end, label))
        cursor += timedelta(hours=1)
    return windows


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def target_money(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def build_message(snapshot: ReportSnapshot, cfg: Config) -> str:
    """Build the final compact Telegram layout.

    The whole report is rendered inside an HTML <pre> block so Telegram uses a
    monospace font and all hourly columns stay vertically aligned. Future
    (not-yet-closed) hours are never included because snapshot.buckets only
    contains closed hourly windows.
    """
    from html import escape

    lines = [
        "Syabas99 Deposit Report",
        "",
        f"Date : {snapshot.report_date.strftime('%d/%m/%Y')}",
        f"Target : {cfg.target_count:,}",
        f"Target Deposit : {target_money(cfg.target_deposit)}",
        "",
        f"TOTAL COUNT : {snapshot.totals.count:,}",
        f"TOTAL AMOUNT : RM {money(snapshot.totals.amount)}",
        "",
        "",
    ]

    run_count = 0
    run_amount = Decimal("0.00")
    for bucket in snapshot.buckets:
        run_count += bucket.totals.count
        run_amount += bucket.totals.amount
        # No table header and no repeated TOTAL label. Fixed widths keep every
        # column aligned even when values grow from hundreds to thousands.
        lines.append(
            f"{bucket.label}  "
            f"{bucket.totals.count:>5,}   "
            f"RM {bucket.totals.amount:>10,.2f}   "
            f"{run_count:>5,} / RM {run_amount:>10,.2f}"
        )

    if cfg.show_validation_status:
        lines += ["", "Validation : cumulative OK" if snapshot.cumulative_verified else "Validation : FAILED"]
        if snapshot.daily_verified is True:
            lines.append("Daily check : OK")
        elif snapshot.daily_verified is False:
            lines.append("Daily check : pending/mismatch")

    # HTML parse mode is used by TelegramBot.upsert_daily_report(). Escaping
    # first makes the generated message safe even if future labels change.
    text = "\n".join(lines).rstrip()
    return f"<pre>{escape(text)}</pre>"
