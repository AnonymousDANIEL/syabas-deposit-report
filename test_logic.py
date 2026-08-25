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


def test_final_aligned_layout():
    cfg = SimpleNamespace(
        target_count=8300,
        target_deposit=Decimal("100000"),
        show_validation_status=False,
    )
    start = datetime(2026, 8, 24, 0, 0, tzinfo=TZ)
    buckets = [
        HourBucket(start, start, "0100", Totals(count=197, amount=Decimal("6424.50"))),
        HourBucket(start, start, "0200", Totals(count=198, amount=Decimal("7074.46"))),
    ]
    snap = ReportSnapshot(
        report_date=date(2026, 8, 24),
        buckets=buckets,
        totals=Totals(count=395, amount=Decimal("13498.96")),
        cumulative_verified=True,
        daily_verified=None,
    )
    msg = build_message(snap, cfg)
    assert msg.startswith("<pre>") and msg.endswith("</pre>")
    assert "HOUR" not in msg
    assert "TOTAL:" not in msg
    assert "0100    197   RM   6,424.50" in msg
    assert "0200    198   RM   7,074.46" in msg


if __name__ == "__main__":
    test_2105()
    test_midnight_finalizes_previous_day()
    test_new_day_starts_new_report_date()
    test_final_aligned_layout()
    print("logic + format tests OK")
