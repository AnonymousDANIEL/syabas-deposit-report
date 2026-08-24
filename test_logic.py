from datetime import datetime
from zoneinfo import ZoneInfo

from report import bucket_windows, closed_report_window

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


if __name__ == "__main__":
    test_2105()
    test_midnight_finalizes_previous_day()
    print("logic tests OK")
