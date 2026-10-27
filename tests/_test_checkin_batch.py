"""签到层优化 P1/P2/P3 的调度器侧契约（Lead 自测）。

覆盖：
- P1-1 先算「要不要干活」再决定等待：空转一轮不睡；
- P1-2 中文任务批量化（并发/错峰/截止时间）与 concurrency=1 的旧行为；
- P2-4 调度日志落盘 logs/tasks-YYYYMMDD.log + 每轮汇总行；
- P3-1 启动补跑只在启动首轮生效，不破坏整点门禁；
- P3-3 签到瞬态失败按 checkin_retries 指数退避重试；
- P0-3 checkin_enabled=false 只停签到，不停上报/旅行。
"""
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_scheduler
import wb_proxy


class StubPool(object):
    def __init__(self, directory, accounts=None):
        self.dir = directory
        self.accounts = list(accounts or [])


class FakeAccount(object):
    """可控的账号替身：签到回执按顺序返回，便于验证重试与计数。"""

    def __init__(self, uid, realm="cn", need_checkin=True, need_report=False,
                 checkin_results=None):
        self.uid = uid
        self.realm = realm
        self.tasks_enabled = True
        self.restricted_until = 0.0
        self.expires_at = 0.0
        self.last_error = ""
        self.need_checkin = need_checkin
        self.need_report = need_report
        self.checkins = 0
        self.reports = 0
        self.refreshes = 0
        self._results = list(checkin_results or [{"ok": True, "msg": "签到成功"}])

    def task_active(self):
        return self.tasks_enabled

    def can_checkin(self):
        return self.need_checkin

    def checkin(self, trigger=""):
        self.checkins += 1
        index = min(self.checkins, len(self._results)) - 1
        return dict(self._results[index])

    def can_report_activity(self):
        return self.need_report

    def report_activity(self):
        self.reports += 1
        return {"ok": True, "msg": "活跃上报成功"}

    def can_daily_chat(self):
        return False

    def refresh(self):
        self.refreshes += 1
        return True


def stamp(hour, minute=0):
    return time.struct_time((2026, 10, 26, hour, minute, 0, 0, 0, -1))


class TimeShim(object):
    """只替换 wb_scheduler 模块眼里的 time：localtime 固定、sleep 记账。

    不能直接改全局 time 模块的属性——那会把 strftime/io 这些也一起打歪，
    测试就会在「日志为什么没落盘」这种地方绕圈。
    """

    def __init__(self, hour=21):
        self.hour = hour
        self.sleeps = []

    def __getattr__(self, name):
        return getattr(time, name)

    def localtime(self, *args):
        return stamp(self.hour)

    def sleep(self, seconds=0.0):
        self.sleeps.append(seconds)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sched-batch-")
        self.dir = self._tmp.name
        self.accounts_dir = os.path.join(self.dir, "accounts")
        os.makedirs(self.accounts_dir)
        self.clock = TimeShim()
        patcher = mock.patch.object(wb_scheduler, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.travel = []
        self.bonus = []
        self.night = []
        for target, name, bucket in (
                (wb_scheduler, "do_cat_travel", self.travel),
                (wb_scheduler.wb_tasks, "run_streak_bonus", self.bonus),
                (wb_scheduler.wb_tasks, "run_night_growth", self.night)):
            recorder = (lambda store: lambda acc: store.append(acc) or {"logs": []})(bucket)
            patcher = mock.patch.object(target, name, recorder)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    @property
    def sleeps(self):
        return self.clock.sleeps

    def scheduler(self, accounts, hour=21):
        self.clock.hour = hour
        pool = StubPool(self.accounts_dir, accounts)
        sched = wb_scheduler.Scheduler(pool)
        sched.log = lambda msg, *a, **k: None
        sched._append_task_log = lambda entry: None
        sched.checkin_hours = [9, 21]
        sched.travel_hours = [9, 21]
        sched.keepalive_hours = []
        sched.cat_hours = []
        sched.all_hours = [9, 21]
        self.logs = []
        sched.log = lambda msg, *a, **k: self.logs.append(str(msg))
        return sched

    def run_cycle(self, sched, **kwargs):
        hour = kwargs.pop("hour", None)
        if hour is not None:
            self.clock.hour = hour
        with mock.patch.object(wb_proxy, "restriction_hold_remaining",
                               return_value=0.0):
            sched._run_cycle("测试巡检", **kwargs)


class IdleCostTests(Base):
    def test_an_idle_cycle_never_sleeps(self):
        accounts = [FakeAccount("u%03d" % i, need_checkin=False) for i in range(24)]
        sched = self.scheduler(accounts)
        sched.travel_hours = []
        sched._tasks_concurrency = lambda: 3
        self.run_cycle(sched)
        self.assertEqual(self.sleeps, [], "没有账号要干活就不该有任何等待")
        summary = [l for l in self.logs if l.startswith("巡检汇总")]
        self.assertEqual(len(summary), 1)
        self.assertIn("候选 0", summary[0])
        self.assertIn("already_today=24", summary[0])

    def test_candidates_are_planned_before_any_waiting(self):
        accounts = [FakeAccount("u%03d" % i, need_checkin=(i % 2 == 0))
                    for i in range(10)]
        sched = self.scheduler(accounts)
        sched.travel_hours = []
        sched._tasks_concurrency = lambda: 3
        self.run_cycle(sched)
        attempted = [a for a in accounts if a.checkins]
        self.assertEqual(len(attempted), 5)
        self.assertTrue(all(v <= 2.0 for v in self.sleeps))


class BatchTests(Base):
    def test_batch_runs_every_account_and_counts(self):
        accounts = [FakeAccount("u%03d" % i, need_report=True) for i in range(9)]
        sched = self.scheduler(accounts)
        sched.travel_hours = []
        sched._tasks_concurrency = lambda: 3
        self.run_cycle(sched)
        self.assertEqual([a.checkins for a in accounts], [1] * 9)
        self.assertEqual([a.reports for a in accounts], [1] * 9)
        summary = [l for l in self.logs if l.startswith("巡检汇总")][0]
        self.assertIn("签到 9/上报 9", summary)

    def test_concurrency_one_keeps_the_serial_jitter(self):
        accounts = [FakeAccount("u%03d" % i) for i in range(4)]
        sched = self.scheduler(accounts)
        sched.travel_hours = []
        sched._tasks_concurrency = lambda: 1
        self.run_cycle(sched)
        jitter = [v for v in self.sleeps if 2.0 <= v <= 5.0]
        self.assertEqual(len(jitter), 3, "三个账号间隔就是三次 2~5 秒抖动")

    def test_concurrent_batch_spreads_the_requests(self):
        accounts = [FakeAccount("u%03d" % i) for i in range(9)]
        sched = self.scheduler(accounts)
        sched.travel_hours = []
        sched._tasks_concurrency = lambda: 4
        self.run_cycle(sched)
        self.assertEqual([a.checkins for a in accounts], [1] * 9)
        stagger = [v for v in self.sleeps if v > 0]
        self.assertTrue(stagger, "并发批次也要错峰，不能同一毫秒打出去")
        self.assertTrue(all(v <= 2.0 for v in stagger))

    def test_batch_deadline_stops_the_runaway(self):
        accounts = [FakeAccount("u%03d" % i) for i in range(30)]
        sched = self.scheduler(accounts)
        sched._tasks_concurrency = lambda: 1
        sched._tasks_budget = lambda: 1
        clock = {"now": 1.0e6}
        calls = {"n": 0}

        def ticking():
            clock["now"] += 0.4
            return clock["now"]

        def slow_tasks(acc, want, deadline=None):
            calls["n"] += 1
            return {"checkin": 1}

        self.clock.time = ticking
        with mock.patch.object(sched, "_run_cn_tasks", side_effect=slow_tasks):
            sched._run_cn_task_batch([(a, {"checkin": True}) for a in accounts])
        self.assertLess(calls["n"], 30, "到点必须停下，不能整夜跑")
        self.assertTrue(any("截止时间" in l for l in self.logs))


class LogFileTests(Base):
    def test_task_log_is_written_next_to_accounts(self):
        accounts = [FakeAccount("u001")]
        sched = self.scheduler(accounts)
        sched.travel_hours = []
        sched._tasks_concurrency = lambda: 3
        # 换回真实的 log()，否则记录器替身会把落盘那一步一起挡掉。
        del sched._append_task_log
        sched.log = type(sched).log.__get__(sched, type(sched))
        self.run_cycle(sched)
        folder = os.path.join(self.dir, "logs")
        # 落盘失败在 log() 里是被吞掉的（日志永远不该拖垮一轮巡检），所以先看内存
        # 环有没有条目，再看目录在不在：直接把 FileNotFoundError 抛出来分不清是
        # 「没写日志」还是「写了但没落盘」。
        self.assertTrue(sched.logs, "这一轮巡检至少要留下一条日志")
        self.assertTrue(os.path.isdir(folder),
                        "日志目录没建起来，内存里有 %d 条：%r"
                        % (len(sched.logs), sched.logs[-3:]))
        names = [n for n in os.listdir(folder) if n.startswith("tasks-")]
        self.assertEqual(len(names), 1)
        with io.open(os.path.join(folder, names[0]), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("巡检汇总", text)
        self.assertIn("自动签到成功", text)


class CatchupTests(Base):
    def test_startup_catchup_runs_travel_after_the_window(self):
        accounts = [FakeAccount("u001", need_checkin=False)]
        sched = self.scheduler(accounts)
        self.run_cycle(sched, hour=20, catchup_tasks=True)
        self.assertEqual(len(self.travel), 1, "09:00 那轮错过时，启动首轮要补旅行")

    def test_regular_cycle_keeps_the_hour_gate(self):
        accounts = [FakeAccount("u001", need_checkin=False)]
        sched = self.scheduler(accounts)
        self.run_cycle(sched, hour=20)
        self.assertEqual(self.travel, [], "整点门禁不能被补跑逻辑带偏（issue #515）")


class RetryTests(Base):
    def test_transient_failure_is_retried_with_backoff(self):
        acc = FakeAccount("u001", checkin_results=[
            {"ok": False, "transient": True, "error": "HTTP 502"},
            {"ok": True, "msg": "签到成功"},
        ])
        sched = self.scheduler([acc])
        sched._checkin_retries = lambda: 2
        sched._tasks_concurrency = lambda: 1
        self.run_cycle(sched)
        self.assertEqual(acc.checkins, 2)
        self.assertIn(5.0, self.sleeps, "第一次退避是 5 秒")
        self.assertTrue(any("重试" in l for l in self.logs))

    def test_hard_failure_is_not_retried(self):
        acc = FakeAccount("u001", checkin_results=[
            {"ok": False, "transient": False, "error": "HTTP 403"},
        ])
        sched = self.scheduler([acc])
        sched._checkin_retries = lambda: 2
        sched._tasks_concurrency = lambda: 1
        self.run_cycle(sched)
        self.assertEqual(acc.checkins, 1)

    def test_retries_respect_the_batch_deadline(self):
        acc = FakeAccount("u001", checkin_results=[
            {"ok": False, "transient": True, "error": "HTTP 502"},
        ])
        sched = self.scheduler([acc])
        sched._checkin_retries = lambda: 5
        result = sched._checkin_with_retry(acc, deadline=time.time() - 1)
        self.assertFalse(result.get("ok"))
        self.assertEqual(acc.checkins, 1, "预算已过就不再重试")


class PauseSwitchTests(Base):
    def test_checkin_paused_still_reports_and_travels(self):
        acc = FakeAccount("u001", need_report=True)
        sched = self.scheduler([acc])
        sched._checkin_enabled = lambda: False
        sched._tasks_concurrency = lambda: 1
        self.run_cycle(sched)
        self.assertEqual(acc.checkins, 0, "签到被暂停")
        self.assertEqual(acc.reports, 1, "上报不受影响")
        self.assertEqual(len(self.travel), 1, "旅行不受影响")
        self.assertTrue(any("checkin_enabled=false" in l for l in self.logs))


class ReloadHookTests(Base):
    def test_reload_result_is_logged(self):
        class Pool(StubPool):
            def reload_if_changed(self):
                return {"changed": True, "added": ["a", "b"], "removed": [],
                        "total": 2}

        pool = Pool(self.accounts_dir, [FakeAccount("u001", need_checkin=False)])
        sched = wb_scheduler.Scheduler(pool)
        self.logs = []
        sched.log = lambda msg, *a, **k: self.logs.append(str(msg))
        sched._append_task_log = lambda entry: None
        sched.travel_hours = []
        sched._tasks_concurrency = lambda: 3
        self.run_cycle(sched)
        self.assertTrue(any("已重新加载" in l for l in self.logs))


class TravelVisibilityTests(Base):
    """P2-4：旅行「无动作」也要计数，否则汇总行的「旅行 N」会被当成覆盖数。

    生产实测（2026-10-26 20:21 那轮）：187 个 cn 活跃号里只有 3 个发生
    depart/claim，其余 184 个在上游是 idle + daily_limit_reached=true
    （今日额度已用完）。旧汇总只写「旅行 3」，读者会以为只有 3 个号参与。
    """

    def test_idle_and_traveling_are_counted_separately(self):
        accounts = [FakeAccount("u%d" % i, need_checkin=False) for i in range(4)]
        sched = self.scheduler(accounts)
        sched._tasks_concurrency = lambda: 2
        answers = {}
        for index, acc in enumerate(accounts):
            answers[acc.uid] = ("depart", "idle", "idle", "traveling")[index]

        def fake_travel(acc):
            action = answers.get(acc.uid, "idle")
            if action == "depart":
                return {"ok": True, "action": "depart", "msg": "猫猫已出发前往「咖啡馆」"}
            if action == "traveling":
                return {"ok": True, "action": "traveling", "msg": "猫猫正在旅行途中"}
            return {"ok": True, "action": "idle",
                    "msg": "猫猫今日已完成旅行，明日 00:00 刷新"}

        with mock.patch.object(wb_scheduler, "do_cat_travel", fake_travel):
            self.run_cycle(sched, hour=21)
        summary = [l for l in self.logs if l.startswith("巡检汇总")][0]
        self.assertIn("旅行 1/", summary)
        self.assertIn("旅行明细 已完成 2/途中 1", summary)

    def test_public_exposes_the_checkin_state_machine(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import wb_accounts
        account = wb_accounts.Account({
            "uid": "uid-travel", "realm": "cn", "enabled": True,
            "lastCheckin": time.strftime("%Y-%m-%d") + " 19:36:23",
            "lastCheckinState": "already",
            "lastCheckinAt": time.strftime("%Y-%m-%d") + " 20:24:08",
            "checkinAttempts": 3,
        })
        row = account.public()
        self.assertEqual(row.get("lastCheckinState"), "already")
        self.assertEqual(row.get("lastCheckinAt"), time.strftime("%Y-%m-%d") + " 20:24:08")
        self.assertEqual(row.get("checkinAttempts"), 3)
        self.assertEqual(row.get("lastCheckin"), time.strftime("%Y-%m-%d") + " 19:36:23")


if __name__ == "__main__":
    unittest.main(verbosity=2)


