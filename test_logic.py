from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from report import HourBucket, ReportSnapshot, bucket_windows, build_message, closed_report_window
from syabas99 import Totals

TZ = ZoneInfo("Asia/Kuala_Lumpur")


def test_2105():
    d, end = closed_report_window(datetime(2026, 8, 23, 21, 5, tzinfo=TZ), TZ)
    windows = bucket_windows(d, end, TZ)
    assert d.isoformat() == "2026-08-23"
    assert len(windows) == 21
    assert windows[0][2] == "0100"
    assert windows[-1][2] == "2100"
    assert windows[-1][0].hour == 20


def test_midnight_finalizes_previous_day():
    d, end = closed_report_window(datetime(2026, 8, 24, 0, 5, tzinfo=TZ), TZ)
    windows = bucket_windows(d, end, TZ)
    assert d.isoformat() == "2026-08-23"
    assert len(windows) == 24
    assert windows[-1][2] == "0000"
    assert windows[-1][0].hour == 23


def test_new_day_starts_new_report_date():
    d, end = closed_report_window(datetime(2026, 8, 24, 1, 5, tzinfo=TZ), TZ)
    windows = bucket_windows(d, end, TZ)
    assert d.isoformat() == "2026-08-24"
    assert len(windows) == 1
    assert windows[0][2] == "0100"


def test_exact_v7_layout_and_future_zero_rows():
    cfg = SimpleNamespace(
        target_count=8300,
        target_deposit=Decimal("100000"),
        show_validation_status=False,
    )
    start = datetime(2026, 8, 25, 0, 0, tzinfo=TZ)
    buckets = [
        HourBucket(start, start, "0100", Totals(count=200, amount=Decimal("8183.41"))),
        HourBucket(start, start, "0200", Totals(count=245, amount=Decimal("10855.55"))),
    ]
    snap = ReportSnapshot(
        report_date=date(2026, 8, 25),
        buckets=buckets,
        totals=Totals(count=445, amount=Decimal("19038.96")),
        cumulative_verified=True,
        daily_verified=None,
    )
    msg = build_message(snap, cfg)

    # No large Telegram code block => no Copy button.
    assert "<pre>" not in msg and "</pre>" not in msg and "<code>" not in msg
    # Exact requested separators/labels are back.
    assert "0100 -  200 | RM   8,183.41 | TOTAL:  200 / RM 8,183.41" in msg
    assert "0200 -  245 | RM  10,855.55 | TOTAL:  445 / RM 19,038.96" in msg
    # All future slots are present as zero hourly rows, but cumulative freezes.
    assert "0300 -    0 | RM       0.00 | TOTAL:  445 / RM 19,038.96" in msg
    assert "2300 -    0 | RM       0.00 | TOTAL:  445 / RM 19,038.96" in msg
    assert "0000 -    0 | RM       0.00 | TOTAL:  445 / RM 19,038.96" in msg
    assert msg.count("TOTAL:") == 24


if __name__ == "__main__":
    test_2105()
    test_midnight_finalizes_previous_day()
    test_new_day_starts_new_report_date()
    test_exact_v7_layout_and_future_zero_rows()
    print("logic + V7 format tests OK")
