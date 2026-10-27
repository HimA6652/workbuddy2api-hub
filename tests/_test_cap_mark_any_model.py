"""6004 撞线的事实要落进窗口记录：任何模型都能保住"配额用尽 + 撞线时刻"。

面板区分「该模型配额耗尽（code 6004）」与「被上游临时限流（429）」，靠的
是 public()["modelCooldowns"][].cappedAt。这个字段只来自凭证档的 model_caps，而
model_caps 只覆盖免费模型（FREE_CAP_MODELS，限额联动的范围）——国内版
deepseek-v4.1-flash 是按积分计费的模型，不在名单内，于是它撞 6004 时：

  * 凭证档不写 modelCaps；
  * 面板拿不到 cappedAt，于是把它写成"429 临时限流（60s~10m 的短窗口）"；
  * 真实恢复时刻在 20 小时以后（真机实测：国内版每号约 2 亿 token/天，
    撞线后 6004 给出的是次日凌晨的墙钟）。

这里把"这条窗口是 6004 配额用尽、撞线于何时"跟窗口一起落到
accounts/model_windows.json（不限于免费模型），重启后由
restore_window_cooldowns() 连同冷却一起挂回来。凭证档 model_caps 的范围**不变**
（限额联动仍然只覆盖免费模型）——这是两件事。

    python tests/_test_cap_mark_any_model.py
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_window_probe

MODEL = "deepseek-v4.1-flash"

def write_account(directory, uid, realm="cn"):
    data = {"uid": uid, "realm": realm, "domain": "www.codebuddy.cn",
            "accessToken": "token-" + uid, "refreshToken": "refresh-" + uid,
            "nickname": uid[:8]}
    path = os.path.join(directory, uid + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path

def write_windows(directory, rows):
    payload = {"version": 1,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "windows": rows}
    with open(os.path.join(directory, wb_window_probe.STATE_NAME), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)

def window(uid, model, reset_at, cap=False, cap_at=0.0):
    return {"uid": uid, "model": model, "reset_at": float(reset_at),
            "attempts": 0, "last_try": 0.0, "last_result": "",
            "cap": bool(cap), "cap_at": float(cap_at or 0.0)}

class CapMarkTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-cap-mark-")
        self.uid = "11111111-2222-3333-4444-555555555555"
        write_account(self.dir, self.uid)
        self._reset_probe_cache()

    def tearDown(self):
        self._reset_probe_cache()

    @staticmethod
    def _reset_probe_cache():
        wb_window_probe._state["dir"] = None
        wb_window_probe._state["windows"] = {}

    def _account(self):
        pool = wb_accounts.AccountPool(self.dir, log=lambda _m: None)
        pool.load()
        return pool.get(self.uid)

    def _cooldown_entry(self, account):
        rows = account.public()["modelCooldowns"]
        return next((r for r in rows if r["model"] == MODEL), None)

    def test_a_cap_window_carries_the_cap_moment(self):
        """窗口标记为 6004 时，面板拿得到撞线时刻。"""
        reset_at = time.time() + 6 * 3600
        cap_at = time.time() - 60
        write_windows(self.dir, [window(self.uid, MODEL, reset_at,
                                       cap=True, cap_at=cap_at)])

        entry = self._cooldown_entry(self._account())
        self.assertIsNotNone(entry, "窗口没有挂回模型冷却")
        self.assertEqual(int(cap_at), entry.get("cappedAt"),
                        "面板拿不到撞线时刻，会把配额耗尽说成临时限流")

    def test_a_plain_window_has_no_cap_moment(self):
        """普通 429 仍不带 cappedAt：面板的措辞分工不变。"""
        write_windows(self.dir, [window(self.uid, MODEL, time.time() + 6 * 3600)])

        entry = self._cooldown_entry(self._account())
        self.assertIsNotNone(entry)
        self.assertNotIn("cappedAt", entry)

    def test_the_credential_file_stays_out_of_the_paid_model(self):
        """限额联动的范围不变：收费模型仍不写 modelCaps。"""
        account = self._account()
        self.assertFalse(account.note_model_cap(MODEL, time.time() + 3600),
                        "国内版 deepseek-v4.1-flash 不该写进 modelCaps")
        self.assertEqual({}, account.model_caps)

    def test_schedule_records_the_cap_and_survives_a_reload(self):
        """窗口探针登记时带着 6004 标记，重启后仍然读得回来。"""
        reset_at = time.time() + 6 * 3600
        cap_at = time.time() - 120
        self.assertTrue(wb_window_probe.schedule(self.dir, self.uid, MODEL,
                                               reset_at, cap=True,
                                               cap_at=cap_at))
        with open(os.path.join(self.dir, wb_window_probe.STATE_NAME),
                  encoding="utf-8") as fh:
            stored = json.load(fh)
        row = stored["windows"][0]
        self.assertTrue(row.get("cap"))
        self.assertEqual(int(cap_at), int(row.get("cap_at") or 0))

        self._reset_probe_cache()
        entry = self._cooldown_entry(self._account())
        self.assertIsNotNone(entry)
        self.assertEqual(int(cap_at), entry.get("cappedAt"))

    def test_a_repeated_window_keeps_the_first_record(self):
        """同一个窗口的重复 429 不重写记录，但会补上缺失的 6004 标记。"""
        reset_at = time.time() + 6 * 3600
        wb_window_probe.schedule(self.dir, self.uid, MODEL, reset_at)
        self.assertFalse(wb_window_probe.schedule(self.dir, self.uid, MODEL,
                                                reset_at))
        cap_at = time.time() - 30
        self.assertTrue(wb_window_probe.schedule(self.dir, self.uid, MODEL,
                                               reset_at, cap=True,
                                               cap_at=cap_at))
        row = wb_window_probe.snapshot(self.dir)[0]
        self.assertTrue(row.get("cap"))
        self.assertEqual(int(cap_at), int(row.get("cap_at") or 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)