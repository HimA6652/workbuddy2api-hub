import json
import os
import sys
import tempfile
import time
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

zone = sys.argv[1]
os.environ["WB_USAGE_DAY_BUCKETS"] = "1"
work = tempfile.TemporaryDirectory(prefix="usage-calendar-")
os.environ["ACCOUNTS_DIR"] = work.name
os.environ["WB_PROXY_USAGE_DIR"] = work.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_proxy

# Windows CPython ignores TZ and has no time.tzset. Supply real IANA wall-clock
# conversions explicitly, so this test cannot silently pass in Asia/Shanghai.
zone_info = ZoneInfo(zone)
zone_cases = {
    "America/New_York": [("2026-11-01", "2026-11-02", "2026-11-03", 49 * 3600),
                         ("2026-03-08", "2026-03-09", "2026-03-10", 47 * 3600)],
    "Europe/Berlin": [("2026-10-25", "2026-10-26", "2026-10-27", 49 * 3600),
                      ("2026-03-29", "2026-03-30", "2026-03-31", 47 * 3600)],
    "Australia/Lord_Howe": [("2026-04-05", "2026-04-06", "2026-04-07", 48.5 * 3600),
                            ("2026-10-04", "2026-10-05", "2026-10-06", 47.5 * 3600)],
}
cases = zone_cases[zone]

def localtime(at=None):
    return datetime.fromtimestamp(time.time() if at is None else at,
                                  zone_info).timetuple()

def mktime(parts):
    date = datetime(*parts[:6], tzinfo=zone_info, fold=parts[8] == 1)
    return date.timestamp()

patches = [mock.patch.object(wb_proxy.time, "localtime", localtime),
           mock.patch.object(wb_proxy.time, "mktime", mktime)]
for patch in patches:
    patch.start()

def at(text):
    day, hour = text.split(" ")
    year, month, date = (int(x) for x in day.split("-"))
    return mktime((year, month, date, int(hour), 0, 0, 0, 0, -1))

for first, middle, last, elapsed in cases:
    assert zone_info.utcoffset(datetime.fromisoformat(first)) != zone_info.utcoffset(
        datetime.fromisoformat(last)), (zone, first, last)
    assert at(last + " 00") - at(first + " 00") == elapsed, (zone, first, last)
    assert wb_proxy._local_midnight(at(middle + " 12")) == at(middle + " 00")

for first, middle, last, elapsed in cases:
    dates = (first, middle, last)
    times = [at(day + " 00") for day in dates]
    log = os.path.join(work.name, "usage.jsonl")
    rows = []
    for day, hour, tokens in ((dates[0], 12, 1), (dates[1], 0, 2),
                              (dates[1], 12, 4), (dates[2], 0, 8)):
        rows.append({"at": at(day + " " + str(hour)), "realm": "cn",
                     "model": "m", "total_tokens": tokens,
                     "outcome": "completed", "credit": 0})
    with open(log, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    lo, hi = times[0], at(dates[2] + " 12")
    for window_lo, window_hi in ((lo, hi), (int(lo), int(hi))):
        got = wb_proxy._usage_timeseries_scan("all", window_lo, window_hi, 86400)
        assert all(isinstance(b["at"], type(window_lo)) for b in got["series"]), (zone, got)
    scan = wb_proxy._usage_timeseries_scan("all", lo, hi, 86400)
    expected = [(day, n) for day, n in zip(dates, (1, 2, 1))]
    actual = [(time.strftime("%Y-%m-%d", time.localtime(b["at"])),
               b["requests"]) for b in scan["series"]]
    assert actual == expected, (zone, actual, expected, scan)
    assert [b["total_tokens"] for b in scan["series"]] == [1, 6, 8]
    assert localtime(scan["series"][1]["at"]).tm_isdst != localtime(
        scan["series"][0]["at"]).tm_isdst, (zone, "fixture did not cross DST")
    assert all(b["at"] == wb_proxy._local_midnight(b["at"])
               for b in scan["series"]), (zone, scan)
    for enabled in (True, False):
        os.environ["WB_USAGE_DAY_BUCKETS"] = "1" if enabled else "0"
        wb_proxy._series_cache.clear()
        wb_proxy._series_state.clear()
        for _ in range(2):
            got = wb_proxy.usage_timeseries(realm="all", range="custom",
                                            since=lo, until=hi,
                                            bucket_seconds=86400, ttl=0)
            assert json.dumps(got, sort_keys=True) == json.dumps(scan, sort_keys=True), (zone, enabled, got, scan)
work.cleanup()
print("calendar-day buckets OK:", zone)
