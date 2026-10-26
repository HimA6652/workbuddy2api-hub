"""任务跑道六个开关的读写契约（规范 §2.1 + P0-3）。

这一组值决定「一次巡检最多占住调度器多久、每个账号最多花几次上游请求、国内签到
到底开不开」，所以每个都有三件事要钉住：

1. 没配置时是内置默认（升级不改行为）；
2. setter 写进去、getter 原样读回来（面板改完立刻生效）；
3. 非法值两条路都不漏 —— **写**报 ValueError（面板手填要能立刻看见错），
   **读**回落默认（手编 settings.json 写坏了不该让巡检失控）。

第 3 条里最容易错的是 bool：它是 int 的子类，JSON 里的 true 会被 ``isinstance(x, int)``
放行成 1，所以每个范围校验都必须先排除 bool。这里逐个覆盖。

Run with: python tests/_test_tasks_settings.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_settings


def _write(directory, data):
    with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _read(directory):
    with open(wb_settings.settings_path(directory), "r", encoding="utf-8") as fh:
        return json.load(fh)


class DefaultsTests(unittest.TestCase):
    """没配置时的内置默认 —— 升级不该悄悄改变现有行为。"""

    def test_defaults_when_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            self.assertEqual(wb_settings.tasks_concurrency(directory), 3)
            self.assertEqual(wb_settings.tasks_budget(directory), 1800)
            self.assertEqual(wb_settings.checkin_retries(directory), 2)
            self.assertEqual(wb_settings.auto_disable_auth_failures(directory), 3)
            self.assertIs(wb_settings.checkin_startup_catchup(directory), True)
            self.assertIs(wb_settings.checkin_enabled(directory), True)

    def test_callers_own_default_still_wins_when_unset(self):
        # 调度器可以带自己的内置默认（比如测试里传 1 模拟串行），没配置时保留它。
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            self.assertEqual(wb_settings.tasks_concurrency(directory, default=1), 1)
            self.assertEqual(wb_settings.tasks_budget(directory, default=60), 60)
            self.assertEqual(wb_settings.checkin_retries(directory, default=0), 0)
            self.assertEqual(
                wb_settings.auto_disable_auth_failures(directory, default=0), 0)

    def test_checkin_enabled_defaults_to_on_without_explicit_dir(self):
        # 只读接口（/tasks/checkin/plan）不拿账号目录也要能读：省略时用 ACCOUNTS_DIR。
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            with mock.patch.dict(os.environ, {"ACCOUNTS_DIR": directory}):
                self.assertIs(wb_settings.checkin_enabled(), True)
            _write(directory, {"checkin_enabled": False})
            with mock.patch.dict(os.environ, {"ACCOUNTS_DIR": directory}):
                self.assertIs(wb_settings.checkin_enabled(), False)


class RoundTripTests(unittest.TestCase):
    """setter → getter → 落盘的 JSON，三处一致。"""

    def test_every_switch_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            self.assertEqual(wb_settings.set_tasks_concurrency(directory, 6), 6)
            self.assertEqual(wb_settings.set_tasks_budget(directory, 900), 900)
            self.assertEqual(wb_settings.set_checkin_retries(directory, 4), 4)
            self.assertIs(
                wb_settings.set_checkin_startup_catchup(directory, False), False)
            self.assertEqual(
                wb_settings.set_auto_disable_auth_failures(directory, 7), 7)
            self.assertIs(wb_settings.set_checkin_enabled(directory, False), False)

            self.assertEqual(wb_settings.tasks_concurrency(directory), 6)
            self.assertEqual(wb_settings.tasks_budget(directory), 900)
            self.assertEqual(wb_settings.checkin_retries(directory), 4)
            self.assertIs(wb_settings.checkin_startup_catchup(directory), False)
            self.assertEqual(wb_settings.auto_disable_auth_failures(directory), 7)
            self.assertIs(wb_settings.checkin_enabled(directory), False)

            stored = _read(directory)
            self.assertEqual(stored["tasks_concurrency"], 6)
            self.assertEqual(stored["tasks_budget"], 900)
            self.assertEqual(stored["checkin_retries"], 4)
            self.assertIs(stored["checkin_startup_catchup"], False)
            self.assertEqual(stored["auto_disable_auth_failures"], 7)
            self.assertIs(stored["checkin_enabled"], False)

    def test_setter_keeps_unrelated_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {"user_custom_top": {"keep": 1},
                               "tasks_concurrency": 4})
            wb_settings.set_tasks_budget(directory, 120)
            stored = _read(directory)
        self.assertEqual(stored["user_custom_top"], {"keep": 1})
        self.assertEqual(stored["tasks_concurrency"], 4)
        self.assertEqual(stored["tasks_budget"], 120)

    def test_the_load_cache_still_invalidates_on_save(self):
        # load() 按 (path, mtime, size) 缓存；setter 走 save() 之后必须读到新值，
        # 否则面板改完开关要等下一次目录 mtime 变化才生效。
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            self.assertEqual(wb_settings.tasks_concurrency(directory), 3)
            wb_settings.set_tasks_concurrency(directory, 8)
            self.assertEqual(wb_settings.tasks_concurrency(directory), 8)
            # 手改文件（同一 size 的另一份内容）也要能读到。
            _write(directory, {"tasks_concurrency": 2})
            self.assertEqual(wb_settings.tasks_concurrency(directory), 2)

    def test_load_returns_a_copy_not_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {"tasks_concurrency": 5})
            snapshot = wb_settings.load(directory)
            snapshot["tasks_concurrency"] = 99
            self.assertEqual(wb_settings.tasks_concurrency(directory), 5)


class RejectBadWritesTests(unittest.TestCase):
    """写入非法值必须当场 ValueError：面板手填的数字要能立刻看见错。"""

    def test_tasks_concurrency_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            self.assertEqual(wb_settings.set_tasks_concurrency(directory, 1), 1)
            self.assertEqual(
                wb_settings.set_tasks_concurrency(
                    directory, wb_settings.TASKS_CONCURRENCY_MAX),
                wb_settings.TASKS_CONCURRENCY_MAX)
            for bad in (0, -1, wb_settings.TASKS_CONCURRENCY_MAX + 1,
                        "3", 3.0, True, None):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        wb_settings.set_tasks_concurrency(directory, bad)

    def test_tasks_budget_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            self.assertEqual(wb_settings.set_tasks_budget(directory, 0), 0)  # 不限制
            self.assertEqual(
                wb_settings.set_tasks_budget(
                    directory, wb_settings.TASKS_BUDGET_MAX),
                wb_settings.TASKS_BUDGET_MAX)
            for bad in (-1, wb_settings.TASKS_BUDGET_MAX + 1, "1800", 1.5, True):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        wb_settings.set_tasks_budget(directory, bad)

    def test_checkin_retries_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            self.assertEqual(wb_settings.set_checkin_retries(directory, 0), 0)
            self.assertEqual(
                wb_settings.set_checkin_retries(
                    directory, wb_settings.CHECKIN_RETRIES_MAX),
                wb_settings.CHECKIN_RETRIES_MAX)
            for bad in (-1, wb_settings.CHECKIN_RETRIES_MAX + 1, "2", False):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        wb_settings.set_checkin_retries(directory, bad)

    def test_auto_disable_auth_failures_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            self.assertEqual(
                wb_settings.set_auto_disable_auth_failures(directory, 0), 0)  # 关闭
            self.assertEqual(
                wb_settings.set_auto_disable_auth_failures(
                    directory, wb_settings.AUTO_DISABLE_AUTH_MAX),
                wb_settings.AUTO_DISABLE_AUTH_MAX)
            for bad in (-1, wb_settings.AUTO_DISABLE_AUTH_MAX + 1, "3", 2.5, True):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        wb_settings.set_auto_disable_auth_failures(directory, bad)

    def test_boolean_switches_coerce_like_the_rest_of_the_file(self):
        # 这两个是 bool 开关，setter 按 truthy 归一（和 daily_chat_startup_catchup 一致）。
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {})
            self.assertIs(wb_settings.set_checkin_enabled(directory, 0), False)
            self.assertIs(wb_settings.set_checkin_enabled(directory, "yes"), True)
            self.assertIs(
                wb_settings.set_checkin_startup_catchup(directory, "no"), True)


class CorruptStoredValueTests(unittest.TestCase):
    """读到的坏值一律回落默认 —— 手编 settings.json 不该把巡检变成突刺。"""

    def test_out_of_range_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {
                "tasks_concurrency": 99,
                "tasks_budget": 999999,
                "checkin_retries": 9,
                "auto_disable_auth_failures": 11,
            })
            self.assertEqual(wb_settings.tasks_concurrency(directory), 3)
            self.assertEqual(wb_settings.tasks_budget(directory), 1800)
            self.assertEqual(wb_settings.checkin_retries(directory), 2)
            self.assertEqual(wb_settings.auto_disable_auth_failures(directory), 3)

    def test_wrong_type_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {
                "tasks_concurrency": "5",
                "tasks_budget": None,
                "checkin_retries": 2.5,
                "auto_disable_auth_failures": [3],
            })
            self.assertEqual(wb_settings.tasks_concurrency(directory), 3)
            self.assertEqual(wb_settings.tasks_budget(directory), 1800)
            self.assertEqual(wb_settings.checkin_retries(directory), 2)
            self.assertEqual(wb_settings.auto_disable_auth_failures(directory), 3)

    def test_json_booleans_are_not_numbers(self):
        # bool 是 int 的子类：不排除的话 {"tasks_concurrency": true} 会读成 1（串行），
        # {"checkin_retries": false} 会读成 0（不重试）——两种都是悄悄降级。
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {
                "tasks_concurrency": True,
                "tasks_budget": False,
                "checkin_retries": True,
                "auto_disable_auth_failures": False,
            })
            self.assertEqual(wb_settings.tasks_concurrency(directory), 3)
            self.assertEqual(wb_settings.tasks_budget(directory), 1800)
            self.assertEqual(wb_settings.checkin_retries(directory), 2)
            self.assertEqual(wb_settings.auto_disable_auth_failures(directory), 3)

    def test_a_zero_budget_is_legal_but_a_zero_concurrency_is_not(self):
        # 0 对 budget 是「不限制」这个明确语义；对 concurrency 则没有意义。
        with tempfile.TemporaryDirectory() as directory:
            _write(directory, {"tasks_budget": 0, "tasks_concurrency": 0})
            self.assertEqual(wb_settings.tasks_budget(directory), 0)
            self.assertEqual(wb_settings.tasks_concurrency(directory), 3)

    def test_bad_boolean_switch_stays_on(self):
        # 坏值只能让功能保持开启，不能悄悄把签到停掉 —— 这条最要命。
        with tempfile.TemporaryDirectory() as directory:
            for bogus in ("false", 0, [], {}, None):
                with self.subTest(bogus=bogus):
                    _write(directory, {"checkin_enabled": bogus,
                                       "checkin_startup_catchup": bogus})
                    self.assertIs(wb_settings.checkin_enabled(directory), True)
                    self.assertIs(
                        wb_settings.checkin_startup_catchup(directory), True)
            _write(directory, {"checkin_enabled": False,
                               "checkin_startup_catchup": False})
            self.assertIs(wb_settings.checkin_enabled(directory), False)
            self.assertIs(wb_settings.checkin_startup_catchup(directory), False)

    def test_a_missing_settings_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIs(wb_settings.checkin_enabled(directory), True)
            self.assertEqual(wb_settings.tasks_concurrency(directory), 3)


if __name__ == "__main__":
    unittest.main()
