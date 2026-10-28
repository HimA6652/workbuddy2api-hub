import json
import os
import sys
import tempfile
import time

zone = sys.argv[1]
os.environ["WB_USAGE_DAY_BUCKETS"] = "1"
work = tempfile.TemporaryDirectory(prefix="usage-calendar-")
os.environ["ACCOUNTS_DIR"] = work.name
os.environ["WB_PROXY_USAGE_DIR"] = work.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_proxy

def at(text):
    day, hour = text.split(" ")
    year, month, date = (int(x) for x in day.split("-"))
    return time.mktime((year, month, date, int(hour), 0, 0, 0, 0, -1))

for dates in (("2026-11-01", "2026-11-02", "2026-11-03"),
              ("2026-03-08", "2026-03-09", "2026-03-10")):
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
    scan = wb_proxy._usage_timeseries_scan("all", lo, hi, 86400)
    expected = [(day, n) for day, n in zip(dates, (1, 2, 1))]
    actual = [(time.strftime("%Y-%m-%d", time.localtime(b["at"])),
               b["requests"]) for b in scan["series"]]
    assert actual == expected, (zone, actual, expected, scan)
    assert [b["total_tokens"] for b in scan["series"]] == [1, 6, 8]
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
