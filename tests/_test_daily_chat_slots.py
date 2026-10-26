
"""网页活跃时段状态落盘（R7）。

背景：Scheduler._daily_chat_done 以前只活在内存里。进程在时段窗口内重启（含
热切换）后集合为空，同一个时段会被再触发一轮：当天已成功的账号有
can_daily_chat() 挡着，没成功的那批会被重新排队，实测一次多重跑 5~9 分钟。

要钉住的行为：

  1. 时段命中后状态立刻落盘（accounts/daily-chat-slots.json，{day, slots}）；
  2. 同一台机器上的新 Scheduler（= 重启 / 热切换后的进程）不再重复触发；
  3. 状态文件里的日期不是今天 → 视为未跑，正常触发；
  4. 状态文件损坏 → 不炸，按未跑处理；
  5. 不写盘目录（pool.dir 为空）也不影响触发逻辑。

Run with the current interpreter (python tests/run_all.py dailychatslots).
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

_TMP = tempfile.mkdtemp(prefix="wb-slots-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_scheduler as S


class StubPool(object):
    def __init__(self, directory):
        self.dir = directory
        self.accounts = []


def make_scheduler(directory):
    sched = S.Scheduler(StubPool(directory))
    sched.log = lambda *a, **k: None
    sched.daily_chat_times = ["09:00"]
    return sched


def slot_time(hour=9, minute=5, day=(2026, 10, 26)):
    return time.struct_time((day[0], day[1], day[2], hour, minute, 0, 0, 300, 0))


class DailyChatSlotStateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pool-", dir=_TMP)
        self.path = os.path.join(self.dir, S.DAILY_CHAT_SLOT_STATE)

    def test_slot_hit_writes_state(self):
        sched = make_scheduler(self.dir)
        with unittest.mock.patch("time.strftime", return_value="2026-10-26"):
            slot = sched._due_daily_chat_slot(slot_time())
        self.assertEqual(slot, "09:00")
        with io.open(self.path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload, {"day": "2026-10-26", "slots": ["09:00"]})

    def test_restart_does_not_replay_the_slot(self):
        with unittest.mock.patch("time.strftime", return_value="2026-10-26"):
            first = make_scheduler(self.dir)
            self.assertEqual(first._due_daily_chat_slot(slot_time()), "09:00")
            # 新进程（热切换后的新 Scheduler）读同一份盘上状态
            second = make_scheduler(self.dir)
            self.assertEqual(second._due_daily_chat_slot(slot_time()), "")
            self.assertIn("09:00", second._daily_chat_done.get("2026-10-26", set()))

    def test_other_day_state_is_ignored(self):
        with io.open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"day": "2026-10-25", "slots": ["09:00"]}, fh)
        with unittest.mock.patch("time.strftime", return_value="2026-10-26"):
            sched = make_scheduler(self.dir)
            self.assertEqual(sched._due_daily_chat_slot(slot_time()), "09:00")

    def test_corrupt_state_falls_back_to_empty(self):
        with io.open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with unittest.mock.patch("time.strftime", return_value="2026-10-26"):
            sched = make_scheduler(self.dir)
            self.assertEqual(sched._due_daily_chat_slot(slot_time()), "09:00")

    def test_outside_grace_window_neither_triggers_nor_writes(self):
        with unittest.mock.patch("time.strftime", return_value="2026-10-26"):
            sched = make_scheduler(self.dir)
            late = slot_time(hour=23, minute=0)
            self.assertEqual(sched._due_daily_chat_slot(late), "")
        self.assertFalse(os.path.exists(self.path))

    def test_no_directory_still_triggers(self):
        sched = make_scheduler("")
        with unittest.mock.patch("time.strftime", return_value="2026-10-26"):
            self.assertEqual(sched._due_daily_chat_slot(slot_time()), "09:00")
            self.assertEqual(sched._due_daily_chat_slot(slot_time()), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
