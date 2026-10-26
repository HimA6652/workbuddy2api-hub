"""全量刷积分的并发与节奏护栏（CAS 单飞 + 账号间拟人间隔）。

面板有两条路会打「全池刷积分」：账号页的「一键刷新积分」（POST /accounts/credits）
与账号页的自动补快照（同一个端点）、以及 GET /accounts/credits。不带 uid/realm 时
它们对**整个账号池**逐个发上游计费请求 —— 真机 23 个账号就是 23 次上游调用，而它
原本既没有单飞、也没有账号间间隔。

后果是实测出来的，不是推测：网关日志里出现过 10 秒内 9 次全量刷（≈207 次上游调用），
触发条件是「池里有一个账号始终拿不到积分快照」——前端每 15 秒重试一次，每次都全池扫。

这个套件钉住两道护栏，口径对齐隔壁 workbuddy2api-panel 的
internal/scheduler/staggered.go：

1. **CAS 单飞**：同一时刻只允许一次全量刷，重复并发直接拒（HTTP 200 + busy=true）。
   注意「拒」不等于「错」——调用方仍能拿到当前快照，只是这次不刷。
2. **账号间 800~1200ms 拟人间隔**：避免几十个账号在同一秒把上游打成一串。
   单账号刷新（带 uid）不吃护栏：它本来就只发一次请求，点了就该立刻有反应。

间隔可用 WB_CREDITS_SWEEP_GAP_MS 覆盖（"min-max" 或单个数字，0 = 关掉）。这个开关
必须容错：环境变量写坏了要回默认，而不是让整个功能不可用。

Run with: python tests/run_all.py credits_sweep
"""
import inspect
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy

GAP_ENV = "WB_CREDITS_SWEEP_GAP_MS"


class FakeAccount(object):
    """只要 fetch_credits() 返回 dict 就够 —— 护栏本身不碰账号内部。"""

    def __init__(self, uid, ok=True, boom=False):
        self.uid = uid
        self.credits = {"remain": 1, "updated_at": 1}
        self._ok = ok
        self._boom = boom
        self.calls = 0

    def fetch_credits(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("boom")
        return {"ok": self._ok, "error": "" if self._ok else "nope"}


class GapSecondsTests(unittest.TestCase):
    """间隔解析：默认值 + 环境变量覆盖 + 坏输入容错。"""

    def setUp(self):
        self._saved = os.environ.pop(GAP_ENV, None)

    def tearDown(self):
        os.environ.pop(GAP_ENV, None)
        if self._saved is not None:
            os.environ[GAP_ENV] = self._saved

    def test_default_is_800_to_1200ms(self):
        self.assertEqual(wb_proxy._credits_sweep_gap_seconds(), (0.8, 1.2))

    def test_env_range(self):
        os.environ[GAP_ENV] = "200-300"
        self.assertEqual(wb_proxy._credits_sweep_gap_seconds(), (0.2, 0.3))

    def test_env_single_value(self):
        os.environ[GAP_ENV] = "500"
        self.assertEqual(wb_proxy._credits_sweep_gap_seconds(), (0.5, 0.5))

    def test_env_reversed_range_is_normalised(self):
        os.environ[GAP_ENV] = "900-100"
        self.assertEqual(wb_proxy._credits_sweep_gap_seconds(), (0.1, 0.9))

    def test_env_garbage_falls_back(self):
        for bad in ("abc", "", "  ", "1-2-3", "--"):
            os.environ[GAP_ENV] = bad
            self.assertEqual(wb_proxy._credits_sweep_gap_seconds(), (0.8, 1.2), bad)

    def test_env_zero_disables_gap(self):
        os.environ[GAP_ENV] = "0"
        self.assertEqual(wb_proxy._credits_sweep_gap_seconds(), (0.0, 0.0))


class SingleFlightTests(unittest.TestCase):
    """CAS 单飞：同一时刻只有一个赢家；释放后能再拿。"""

    def tearDown(self):
        # 无论断言怎么失败，都把标志放回去，免得污染同一进程里的后续用例
        try:
            wb_proxy._credits_sweep_end()
        except Exception:
            pass

    def test_second_begin_is_refused_until_end(self):
        self.assertTrue(wb_proxy._credits_sweep_begin())
        self.assertFalse(wb_proxy._credits_sweep_begin(), "已有一轮在跑时第二次必须被拒")
        wb_proxy._credits_sweep_end()
        self.assertTrue(wb_proxy._credits_sweep_begin(), "释放之后必须能再拿")

    def test_only_one_of_many_threads_wins(self):
        """真并发下也只有一个赢家 —— 这是它挡住「前端每 15 秒重试」的关键。"""
        winners = []
        lock = threading.Lock()
        start = threading.Barrier(16)

        def run():
            start.wait()
            got = wb_proxy._credits_sweep_begin()
            if got:
                with lock:
                    winners.append(got)
                time.sleep(0.05)
                wb_proxy._credits_sweep_end()

        threads = [threading.Thread(target=run) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1, "16 个线程同时抢，只该有一个拿到")


class SweepTests(unittest.TestCase):
    """扫库本身：跳过 None、保持顺序、按需停顿、单个账号炸掉不中断整轮。"""

    def setUp(self):
        self._saved = os.environ.pop(GAP_ENV, None)

    def tearDown(self):
        os.environ.pop(GAP_ENV, None)
        if self._saved is not None:
            os.environ[GAP_ENV] = self._saved

    def test_skips_missing_accounts_and_keeps_order(self):
        a, b = FakeAccount("u1"), FakeAccount("u2")
        res = wb_proxy._credits_sweep([a, None, b], paced=False)
        self.assertEqual([r["uid"] for r in res], ["u1", "u2"])
        self.assertEqual([a.calls, b.calls], [1, 1])
        self.assertTrue(all(r["ok"] for r in res))

    def test_unpaced_does_not_wait(self):
        """单账号 / 关掉间隔时必须保持改造前的即时反应，不能平白多等一秒。"""
        os.environ[GAP_ENV] = "800-1200"
        t0 = time.time()
        wb_proxy._credits_sweep([FakeAccount("u1")], paced=False)
        self.assertLess(time.time() - t0, 0.2)

    def test_paced_inserts_one_gap_between_accounts(self):
        """n 个账号之间有 n-1 个间隔（首尾不算）。

        断言的是**请求了几个间隔、各多长**，不是墙上时钟：真 sleep 的写法要卡
        「>0.20s 且 <0.50s」，CI 机器一忙（两次 sleep 累计抖动 260ms）就假失败。
        """
        os.environ[GAP_ENV] = "120"
        delays = []
        with mock.patch.object(wb_proxy.time, "sleep", delays.append):
            wb_proxy._credits_sweep([FakeAccount("u%d" % i) for i in range(3)], paced=True)
        self.assertEqual(len(delays), 2, "3 个账号该有 2 个间隔，实际 %r" % (delays,))
        for d in delays:
            self.assertGreaterEqual(d, 0.11, "间隔应落在 120ms 附近，实际 %r" % (delays,))
            self.assertLessEqual(d, 0.13, "间隔应落在 120ms 附近，实际 %r" % (delays,))

    def test_paced_gap_uses_random_module(self):
        """默认 800~1200ms 的抖动走 random.uniform；漏 import 会在真实刷积分时炸掉。"""
        os.environ[GAP_ENV] = "800-1200"
        delays = []
        with mock.patch.object(wb_proxy.time, "sleep", delays.append):
            wb_proxy._credits_sweep([FakeAccount("u1"), FakeAccount("u2")], paced=True)
        self.assertEqual(len(delays), 1)
        self.assertGreaterEqual(delays[0], 0.8)
        self.assertLessEqual(delays[0], 1.2)

    def test_zero_gap_env_keeps_it_fast(self):
        os.environ[GAP_ENV] = "0"
        t0 = time.time()
        wb_proxy._credits_sweep([FakeAccount("u%d" % i) for i in range(5)], paced=True)
        self.assertLess(time.time() - t0, 0.2)

    def test_one_bad_account_does_not_abort_the_sweep(self):
        """池里一个坏凭证不该让其余账号全部刷不到（对齐 WBAPI 的 continue）。"""
        bad, good = FakeAccount("bad", boom=True), FakeAccount("good")
        res = wb_proxy._credits_sweep([bad, good], paced=False)
        self.assertEqual(len(res), 2)
        self.assertFalse(res[0]["ok"])
        self.assertEqual(res[0]["error"], "boom")
        self.assertTrue(res[1]["ok"], "坏号后面的好号必须照刷")
        self.assertEqual(good.calls, 1)

    def test_failed_fetch_is_reported_not_raised(self):
        """取数失败（返回 ok=False）走正常回执，不抛。"""
        res = wb_proxy._credits_sweep([FakeAccount("u1", ok=False)], paced=False)
        self.assertFalse(res[0]["ok"])
        self.assertEqual(res[0]["error"], "nope")


class RouteWiringTests(unittest.TestCase):
    """两条路由真的接了护栏，且释放走 finally（异常也不能把锁留在手里）。

    全池刷改成后台线程之后，CAS 单飞与 finally 释放搬进了 _credits_sweep_async：
    handler 只挑目标、回执，谁抢锁、谁在 finally 里放，都在那一个函数里钉住。
    套件因此改盯 _credits_sweep_async —— 把护栏搬走不是放松，换个人绕过同一个坑
    照样应该红。
    """

    def test_get_accounts_credits_runs_in_the_background(self):
        src = inspect.getsource(wb_proxy.Handler._get_accounts_credits)
        self.assertIn("_credits_sweep_async", src,
                      "GET /accounts/credits 必须走后台刷，否则 数百个账号会把请求挂死")
        self.assertIn("busy", inspect.getsource(wb_proxy._credits_sweep_async),
                      "被单飞拒掉时要显式告诉调用方这不是错误")

    def test_background_sweep_still_holds_the_single_flight(self):
        src = inspect.getsource(wb_proxy._credits_sweep_async)
        self.assertIn("_credits_sweep_begin", src)
        self.assertIn("_credits_sweep_end", src)
        self.assertIn("finally", src, "释放必须在 finally 里，否则异常会把 CAS 单飞永久留下")
        self.assertIn("busy", src, "被单飞拒掉时要显式告诉调用方这不是错误")

    def test_post_accounts_credits_only_guards_multi_account(self):
        src = inspect.getsource(wb_proxy.Handler._route_accounts_credits_fetch)
        self.assertIn("_credits_sweep_async", src)
        self.assertIn("multi", src, "单账号刷新不该被别人的全量刷挡住")
        self.assertIn("busy", src)

    def test_sweep_is_the_only_place_that_calls_fetch_credits_in_bulk(self):
        """两条全扫路径都必须走 _credits_sweep —— 直接写 for + fetch_credits 就绕过了护栏。"""
        for name in ("_get_accounts_credits", "_route_accounts_credits_fetch"):
            src = inspect.getsource(getattr(wb_proxy.Handler, name))
            self.assertNotIn("fetch_credits()", src,
                             "%s 不该自己直接 fetch_credits，必须走 _credits_sweep" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
