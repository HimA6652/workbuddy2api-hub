"""账号目录热重载：只有目录签名变了才 load()（规范 §2.2 P3-2）。

调度器每轮开头会调 AccountPool.reload_if_changed()：

- 目录签名 = (json 文件数, 最大 mtime_ns, 总字节数, 文件名集)；签名不变就返回
  {"changed": False, ...}，绝不重载——否则每轮 load() 都会把运行态
  （token_expired / 冷却 / 熔断）清掉；
- 签名变了才 load()，并给出 uid 集合的 added / removed，写一条 self.log；
- 首次调用只记录签名，不重载（池子刚构造完，load() 由调用方决定）。

Run with the current interpreter (python tests/run_all.py hotreload).
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts


def _write(directory, uid, nickname="", mtime_ns=None):
    path = os.path.join(directory, uid + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"uid": uid, "accessToken": "token-" + uid, "realm": "cn",
                   "nickname": nickname}, fh, ensure_ascii=False)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _bump(path, delta_ns=2_000_000_000):
    """把 mtime 推后（写内容之后 mtime 可能落在同一刻度上，签名要能看见变化）。"""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_ns))


class ReloadIfChangedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hotreload-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        self.logs = []
        _write(self.directory, "uid-a", "A")
        _write(self.directory, "uid-b", "B")
        self.pool = wb_accounts.AccountPool(self.directory, log=self.logs.append)
        self.pool.load()

    def _uids(self):
        return sorted(account.uid for account in self.pool.accounts)

    def test_first_call_only_records_the_signature(self):
        result = self.pool.reload_if_changed()
        self.assertFalse(result["changed"])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(self.logs, [])

    def test_unchanged_directory_is_a_no_op(self):
        self.pool.reload_if_changed()
        before = self.pool.accounts
        result = self.pool.reload_if_changed()
        self.assertFalse(result["changed"])
        self.assertEqual(result["checked"], 2)
        self.assertIs(self.pool.accounts, before)   # 没有重建 Account 对象
        self.assertEqual(self.logs, [])

    def test_touching_one_file_reloads_without_uid_changes(self):
        self.pool.reload_if_changed()
        path = _write(self.directory, "uid-a", "A-改过")
        _bump(path)
        result = self.pool.reload_if_changed()
        self.assertTrue(result["changed"])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], [])
        nicknames = {account.uid: account.nickname for account in self.pool.accounts}
        self.assertEqual(nicknames["uid-a"], "A-改过")
        self.assertEqual(len(self.logs), 1)
        self.assertIn("账号目录已变化", self.logs[0])

    def test_signature_is_refreshed_after_the_reload(self):
        self.pool.reload_if_changed()
        _bump(_write(self.directory, "uid-b", "B-改过"))
        self.assertTrue(self.pool.reload_if_changed()["changed"])
        # load() 之后再取一次签名：下一轮不该再重载一次。
        self.assertFalse(self.pool.reload_if_changed()["changed"])
        self.assertEqual(len(self.logs), 1)

    def test_added_file_is_reported(self):
        self.pool.reload_if_changed()
        _write(self.directory, "uid-c", "C", mtime_ns=10_000_000_000_000_000_000)
        result = self.pool.reload_if_changed()
        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], ["uid-c"])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["checked"], 3)
        self.assertEqual(self._uids(), ["uid-a", "uid-b", "uid-c"])

    def test_equal_size_rename_in_the_same_clock_tick_is_seen(self):
        """一删一增、长度相同、mtime 落在同一刻度：签名仍要看出变化。

        生产上面板「改 uid」就是这个形状。Windows 的文件时间戳取自约 15.6ms 的
        时钟中断，两张等长的号卡可以拿到完全相同的 mtime_ns（本套件在 --jobs 4
        下就这样偶发漏判过 changed=false）：只靠「个数 + 最大 mtime + 总字节」是
        看不出区别的，签名里的文件名集兜住它。
        """
        self.pool.reload_if_changed()
        old_path = os.path.join(self.directory, "uid-b.json")
        st = os.stat(old_path)
        os.remove(old_path)
        new_path = _write(self.directory, "uid-z", "Z", mtime_ns=st.st_mtime_ns)
        self.assertEqual(os.path.getsize(new_path), st.st_size)
        result = self.pool.reload_if_changed()
        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], ["uid-z"])
        self.assertEqual(result["removed"], ["uid-b"])

    def test_removed_file_is_reported(self):
        self.pool.reload_if_changed()
        os.remove(os.path.join(self.directory, "uid-b.json"))
        _bump(os.path.join(self.directory, "uid-a.json"))
        result = self.pool.reload_if_changed()
        self.assertTrue(result["changed"])
        self.assertEqual(result["removed"], ["uid-b"])
        self.assertEqual(result["added"], [])
        self.assertEqual(self._uids(), ["uid-a"])

    def test_uid_rename_shows_up_as_one_add_and_one_remove(self):
        self.pool.reload_if_changed()
        os.remove(os.path.join(self.directory, "uid-b.json"))
        _write(self.directory, "uid-z", "Z")
        result = self.pool.reload_if_changed()
        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], ["uid-z"])
        self.assertEqual(result["removed"], ["uid-b"])

    def test_non_account_files_do_not_trigger_a_reload(self):
        self.pool.reload_if_changed()
        with open(os.path.join(self.directory, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"autoDisableAuthFailures": 0}, fh)
        self.assertFalse(self.pool.reload_if_changed()["changed"])
        self.assertEqual(self.logs, [])

    def test_missing_directory_is_not_an_error(self):
        missing = os.path.join(self.directory, "nope")
        pool = wb_accounts.AccountPool(missing, log=self.logs.append)
        self.assertFalse(pool.reload_if_changed()["changed"])
        os.makedirs(missing)
        _write(missing, "uid-x", "X")
        result = pool.reload_if_changed()
        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], ["uid-x"])

    def test_own_save_is_not_a_directory_change(self):
        """自己 save() 落盘（签到 / 刷新 token / 自动停用）不该触发重载。

        否则每轮签到写完文件，下一轮开头就 load() 一次：运行态全清、日志每轮一条，
        P2-3 的连续认证失败计数也永远攒不到阈值。
        """
        self.pool.reload_if_changed()
        account = self.pool.get("uid-a")
        account.token_expired = True
        account.auth_fail_streak = 2
        account.last_checkin = "2026-10-26 19:00:00"
        account.save(self.directory)          # 本进程自己的写盘
        result = self.pool.reload_if_changed()
        self.assertFalse(result["changed"])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(self.logs, [])
        # 运行态与对象身份都还在（没有重建 Account）。
        same = self.pool.get("uid-a")
        self.assertIs(same, account)
        self.assertTrue(same.token_expired)
        self.assertEqual(same.auth_fail_streak, 2)

    def test_own_save_then_external_edit_still_reloads(self):
        """自己写过一次之后，别人再改同一个文件仍然要重载。"""
        self.pool.reload_if_changed()
        self.pool.get("uid-a").save(self.directory)
        self.assertFalse(self.pool.reload_if_changed()["changed"])
        _bump(_write(self.directory, "uid-a", "A-外部改过"))
        result = self.pool.reload_if_changed()
        self.assertTrue(result["changed"])
        self.assertEqual(
            {a.uid: a.nickname for a in self.pool.accounts}["uid-a"], "A-外部改过")

    def test_own_save_of_one_account_leaves_the_others_alone(self):
        self.pool.reload_if_changed()
        self.pool.get("uid-b").save(self.directory)
        self.assertFalse(self.pool.reload_if_changed()["changed"])
        _bump(_write(self.directory, "uid-a", "A-外部改过"))
        result = self.pool.reload_if_changed()
        self.assertTrue(result["changed"])
        self.assertEqual(len(self.logs), 1)

    def test_save_window_race_is_not_a_directory_change(self):
        """os.replace 已生效、登记表还没跟上的那几微秒，不该判成外部改动。

        生产 20:11:04 实测：热重载正好切进 187 个账号持续写盘的窗口（活跃上报 /
        旅行 / 刷新 token 都在 save），判出 changed=true 把整池重建了一遍，
        token_expired / cooldown / auth_fail_streak 全被重置。这里直接写文件模拟
        「文件已换」，再由后台线程隔 30 毫秒登记 _SELF_WRITTEN_FILES——与
        Account.save() 的真实时序一致，_confirm_external() 必须把这一轮洗白。
        """
        self.pool.reload_if_changed()
        account = self.pool.get("uid-a")
        account.auth_fail_streak = 2
        account.token_expired = True
        path = os.path.join(self.directory, "uid-a.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"uid": "uid-a", "accessToken": "token-uid-a",
                       "realm": "cn", "nickname": "A-刚写过"}, fh, ensure_ascii=False)
        _bump(path)                                   # 签名一定看得见这次写
        key = os.path.abspath(path)
        self.addCleanup(wb_accounts._SELF_WRITTEN_FILES.pop, key, None)

        def register_later():
            time.sleep(0.03)                          # save() 登记那一步的滞后
            st = os.stat(path)
            wb_accounts._SELF_WRITTEN_FILES[key] = (st.st_mtime_ns, st.st_size)

        thread = threading.Thread(target=register_later)
        thread.start()
        result = self.pool.reload_if_changed()
        thread.join()

        self.assertFalse(result["changed"])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], [])
        self.assertIs(self.pool.get("uid-a"), account)      # 没有重建 Account
        self.assertEqual(account.auth_fail_streak, 2)       # 运行态还在
        self.assertTrue(account.token_expired)
        self.assertEqual(self.logs, [])

    def test_a_real_external_edit_is_never_swallowed(self):
        """反向用例：登记表永远不更新（真外部改动），复核窗口不能把它吃掉。"""
        self.pool.reload_if_changed()
        self.pool.get("uid-a").auth_fail_streak = 2
        path = _write(self.directory, "uid-a", "A-外部改过")
        _bump(path)
        # 前面若有自己写过的登记（本用例不应有），这里确保它不成立。
        wb_accounts._SELF_WRITTEN_FILES.pop(os.path.abspath(path), None)
        started = time.time()
        result = self.pool.reload_if_changed()
        elapsed = time.time() - started
        self.assertTrue(result["changed"])
        # 确实走了复核（最多 3 次、每次隔 50ms），不是第一次就放行。
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("账号目录已变化", self.logs[0])
        self.assertEqual(self.pool.get("uid-a").auth_fail_streak, 0)

    def test_reload_resets_runtime_state_of_accounts(self):
        """文件变了就以磁盘为准：token_expired 这类运行态随之清掉。"""
        self.pool.reload_if_changed()
        self.pool.get("uid-a").token_expired = True
        _bump(_write(self.directory, "uid-a", "A"))
        self.assertTrue(self.pool.reload_if_changed()["changed"])
        self.assertFalse(self.pool.get("uid-a").token_expired)


    def test_models_dev_cache_is_not_a_directory_change(self):
        """model.json（models.dev 索引缓存）刷新不能算「目录被外部改动」。

        生产 20:27 实测：wb_modelsdev.save_index() 把索引写进 accounts/model.json，
        reload_if_changed() 判成外部改动 → changed=true、added/removed 全空 →
        load() 重建整池，冷却 / 熔断 / 签到状态机全被清掉。缓存不是账号文件，
        必须在 NON_ACCOUNT_FILES 里被跳过。
        """
        self.assertIn("model.json", wb_accounts.NON_ACCOUNT_FILES)
        self.pool.reload_if_changed()
        cache = os.path.join(self.directory, "model.json")
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"models": {"demo-model": {"id": "demo-model"}}}, fh)
        _bump(cache, 5_000_000_000)
        result = self.pool.reload_if_changed()
        self.assertFalse(result["changed"])
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(self.logs, [])
        # 账号对象没被重建。
        self.assertEqual(self._uids(), ["uid-a", "uid-b"])

    def test_models_dev_cache_does_not_mask_a_real_account_edit(self):
        """跳过缓存文件之后，真正的账号改动仍然要重载。"""
        self.pool.reload_if_changed()
        cache = os.path.join(self.directory, "model.json")
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"models": {}}, fh)
        _bump(cache, 5_000_000_000)
        self.assertFalse(self.pool.reload_if_changed()["changed"])
        _bump(_write(self.directory, "uid-b", "B-外部改过"), 7_000_000_000)
        result = self.pool.reload_if_changed()
        self.assertTrue(result["changed"])
        self.assertEqual({a.uid: a.nickname for a in self.pool.accounts}["uid-b"], "B-外部改过")


if __name__ == "__main__":
    unittest.main()
