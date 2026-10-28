# IANA timezone fixtures

These three binary TZif files are test-only inputs for
`_usage_calendar_days_probe.py`. They keep the DST/calendar-day regression
executable on Windows CPython installations without a system IANA database or
the optional `tzdata` package.

Source: `tzdata` 2026.3 (IANA Time Zone Database 2026c).

- `America/New_York`: 1,744 bytes; SHA-256 `d7f2206b3a45989fc9ad63d558922532fa7352280d5f87176bf1db79cb1d1fa9`
- `Europe/Berlin`: 705 bytes; SHA-256 `a7fd9932d785d4d690900b834c3563c1810c1cf2e01711bcc0926af6c0767cb7`
- `Australia/Lord_Howe`: 692 bytes; SHA-256 `f368bd25659c0293d02bb79ec7dac7d5b73a92dffafce14b4dd2ffb8ba11aada`

The containing test passes the directory through `PYTHONTZPATH`. These files
are not an application timezone database and do not add an application runtime
dependency.

Upstream data and license: <https://github.com/eggert/tz/tree/2026c>.
Unless otherwise specified, the IANA code and data (including its LICENSE
file) are in the public domain; the named BSD-covered C source/manual files
are not included here.
