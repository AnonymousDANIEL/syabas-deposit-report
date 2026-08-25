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
    """Build the final Telegram layout as plain text.

    No HTML <pre> / <code> is used, so Telegram will not show a Copy button.
    All 24 slots are always visible in this order: 0100..2300,0000.

    Closed slots show their real hourly values and real cumulative TOTAL.
    Future slots show zero for BOTH the hourly values and the TOTAL column.
    When that hour closes, the next refresh replaces the zero row with the
    newly calculated real values.
    """
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
    ]

    by_label = {bucket.label: bucket for bucket in snapshot.buckets}
    labels = [f"{hour:02d}00" for hour in range(1, 24)] + ["0000"]

    run_count = 0
    run_amount = Decimal("0.00")

    for label in labels:
        bucket = by_label.get(label)

        if bucket is None:
            # Future/not-yet-closed hour: everything on this row stays zero.
            hour_count = 0
            hour_amount = Decimal("0.00")
            display_total_count = 0
            display_total_amount = Decimal("0.00")
        else:
            hour_count = bucket.totals.count
            hour_amount = bucket.totals.amount
            run_count += hour_count
            run_amount += hour_amount
            display_total_count = run_count
            display_total_amount = run_amount

        lines.append(
            f"{label} - "
            f"{hour_count:>4,} | "
            f"RM {hour_amount:>10,.2f} | "
            f"TOTAL: {display_total_count:>4,} / RM {display_total_amount:,.2f}"
        )

    if cfg.show_validation_status:
        lines += ["", "Validation : cumulative OK" if snapshot.cumulative_verified else "Validation : FAILED"]
        if snapshot.daily_verified is True:
            lines.append("Daily check : OK")
        elif snapshot.daily_verified is False:
            lines.append("Daily check : pending/mismatch")

    return "\n".join(lines).rstrip()
