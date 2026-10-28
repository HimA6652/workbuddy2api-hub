"""整点巡检的触发窗口：整点那一分钟被占用时不能整天漏掉。

旧实现只在 cur_min == 0 那一分钟检查一次：循环正在跑长轮次、或者进程刚好在
热切换，这个整点轮当天就再也不会跑（22:00 的 Token 保活就这样漏过一次）。
"""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-hour-tick-")

import wb_scheduler  # noqa: E402


def at(hour, minute, day=27):
    return time.struct_time((2026, 10, day, hour, minute, 0, 0, 0, -1))


class StubPool(object):
    def __init__(self, directory):
        self.dir = directory
        self.accounts = []


class HourTickTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hour-tick-")
        self.sch = wb_scheduler.Scheduler(StubPool(self.dir))
        self.sch.checkin_hours = [9, 21]
        self.sch.travel_hours = [9, 21]
        self.sch.keepalive_hours = [22]
        self.sch.cat_hours = [1]
        self.sch.all_hours = [1, 9, 21, 22]

    def test_fires_on_the_hour_when_idle(self):
        self.assertTrue(self.sch._due_hour_cycle(at(9, 0)))

    def test_fires_anywhere_inside_the_hour(self):
        self.assertTrue(self.sch._due_hour_cycle(at(9, 7)))
        self.assertTrue(self.sch._due_hour_cycle(at(9, 59)))
        self.assertTrue(self.sch._due_hour_cycle(at(22, 41)))

    def test_only_once_per_hour(self):
        self.sch._last_hour_fire = self.sch._hour_fire_key(at(9, 7))
        self.assertFalse(self.sch._due_hour_cycle(at(9, 8)))
        self.assertFalse(self.sch._due_hour_cycle(at(9, 59)))
        self.assertTrue(self.sch._due_hour_cycle(at(21, 0)))

    def test_next_day_fires_again(self):
        self.sch._last_hour_fire = self.sch._hour_fire_key(at(9, 7, day=27))
        self.assertTrue(self.sch._due_hour_cycle(at(9, 1, day=28)))

    def test_unconfigured_hour_never_fires(self):
        self.assertFalse(self.sch._due_hour_cycle(at(3, 0)))
        self.assertFalse(self.sch._due_hour_cycle(at(23, 30)))

    def test_startup_cycle_respects_handover_hold(self):
        calls = []
        def execute_cycle(*args, **kwargs):
            calls.append((args, kwargs))
        self.sch._execute_cycle = execute_cycle
        self.sch._run_window_probes = lambda: None
        self.sch._run_quota_scans = lambda: None
        self.sch.enabled = False
        self.sch._stop_event.set()
        with unittest.mock.patch.object(wb_scheduler.time, "sleep", return_value=None):
            self.sch._run_loop()
        self.assertEqual(calls, [])
        self.assertTrue(any("跳过启动初次初始化巡检" in line for line in self.sch.logs))

    def test_startup_cycle_recovers_after_hold_released(self):
        calls = []
        self.sch._execute_cycle = lambda *args, **kwargs: calls.append((args, kwargs))
        self.sch._run_window_probes = lambda: None
        self.sch._run_quota_scans = lambda: None
        self.sch.enabled = False
        waits = []
        def finish_after_recovery(timeout):
            waits.append(timeout)
            self.sch.enabled = True
            if len(waits) >= 2:
                self.sch._stop_event.set()
            return False
        self.sch._stop_event.wait = finish_after_recovery
        with unittest.mock.patch.object(wb_scheduler.time, "sleep", return_value=None), \
                unittest.mock.patch.object(self.sch, "_due_daily_chat_slot", return_value=None), \
                unittest.mock.patch.object(self.sch, "_due_hour_cycle", return_value=False):
            self.sch._run_loop()
        self.assertEqual(calls, [(("启动初次初始化巡检",), {"catchup_tasks": True})])
        self.assertFalse(self.sch._startup_cycle_pending)

    def test_startup_cycle_still_runs_normally(self):
        calls = []
        def execute_cycle(*args, **kwargs):
            calls.append((args, kwargs))
        self.sch._execute_cycle = execute_cycle
        self.sch._run_window_probes = lambda: None
        self.sch._run_quota_scans = lambda: None
        self.sch._stop_event.set()
        with unittest.mock.patch.object(wb_scheduler.time, "sleep", return_value=None):
            self.sch._run_loop()
        self.assertEqual(calls, [(("启动初次初始化巡检",), {"catchup_tasks": True})])

    def test_full_cycle_consumes_the_hour(self):
        self.sch._execute_cycle("整点排程命中 (9:00)")
        self.assertEqual(self.sch._last_hour_fire, self.sch._hour_fire_key())
        self.assertFalse(self.sch._due_hour_cycle(time.localtime()))

    def test_daily_chat_only_cycle_keeps_the_hour(self):
        before = self.sch._last_hour_fire
        self.sch._execute_cycle("网页活跃定时命中 (20:35)", daily_chat_only=True)
        self.assertEqual(self.sch._last_hour_fire, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
