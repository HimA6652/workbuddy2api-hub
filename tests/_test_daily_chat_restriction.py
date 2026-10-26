
"""每日活跃腿的 403 / code 11140：按账号停手、落盘，且不误触整池暂停。

背景（2026-10-26 生产实测）：intl 侧 169 个启用号里有 158 个在「每日活跃对话」
上收到 403 / code 11140，但 /accounts 的 restrictedFor 全是 0 —— 这条腿当时只
return 一个错误串，既不调 note_restricted 也不落盘。结果是调度器的「跳过受限
账号」闸门永远看不到它们：158 个号 × 每轮 6 个 slot = 一天 961 次无意义 403，
并且整池连坐暂停也永不触发。

要钉住的行为：

  1. 403 + 11140 → restricted=True、restricted_until ≈ +2h、last_error 标记、
     restrictedUntil 写进账号 json（重启不丢）；
  2. 限制期内的账号 can_daily_chat() 为 False，daily_chat() 直接跳过、一个上游
     请求都不发；
  3. 这条腿**不**记整池连坐事件（restriction_hold_remaining 保持 0）——离线打卡
     反复撞上早已被封的号，不该把还能用的号一起停掉；
  4. 429 / code 14017（试用版未激活）不上风控限制，只记错误；正文保留到 400 字符，
     面板/日志能看到 displayMsg；
  5. 内容审核类 403（如 10001）走原路径，不给账号上长冷却。

Run with the current interpreter (python tests/run_all.py dailychat).
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error

_TMP = tempfile.mkdtemp(prefix="wb-dailychat-restrict-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy as P


RESTRICT_BODY = json.dumps({
    "code": 11140, "msg": "request illegal", "requestId": "1" * 32,
    "displayMsg": {
        "en": "Your account has been restricted due to unusual activity",
        "zh": "账号存在异常行为，已被限制使用",
    },
}).encode("utf-8")

TRIAL_BODY = json.dumps({
    "error": {"data": {"code": 14017,
                       "msg": "The trial version is not yet activated. Please log out of your account and log in again."}}
}).encode("utf-8")

CONTENT_BODY = json.dumps({"code": 10001, "msg": "content rejected"}).encode("utf-8")


def http_error(code, body):
    return urllib.error.HTTPError("https://upstream/chat", code, "err", {},
                                  io.BytesIO(body))


class RaisingUpstream(object):
    """每次出站都按 script 抛错 / 应答；calls 记录真实出站次数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None, proxy=None):
        self.calls.append(req.full_url)
        code, body = self.script.pop(0) if self.script else (200, b"{}")
        if code >= 400:
            raise http_error(code, body)
        resp = io.BytesIO(body)
        resp.status = code
        resp.getcode = lambda: code
        resp.headers = {}
        return resp


class DailyChatRestrictionTest(unittest.TestCase):
    def setUp(self):
        P._RESTRICTION_EVENTS.clear()
        P._RESTRICTION_HOLD.clear()

    def make_account(self, uid="dc001234", product="workbuddy"):
        directory = tempfile.mkdtemp(prefix="wb-dc-", dir=_TMP)
        path = os.path.join(directory, uid + ".json")
        data = {"uid": uid, "accessToken": "t-" + uid, "realm": "intl",
                "product": product, "enabled": True}
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
        return wb_accounts.Account(data, path=path)

    def run_upstream(self, acc, script):
        upstream = RaisingUpstream(script)
        real = wb_accounts.urlopen
        wb_accounts.urlopen = upstream
        try:
            res = acc._daily_chat_upstream(False)
        finally:
            wb_accounts.urlopen = real
        return upstream, res

    # ---- 1. 11140 → 停手 + 落盘 ----
    def test_restriction_stops_the_account_and_persists(self):
        acc = self.make_account()
        upstream, res = self.run_upstream(acc, [(403, RESTRICT_BODY)])
        self.assertEqual(len(upstream.calls), 1)
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("restricted"))
        self.assertIn("11140", res["error"])
        self.assertIn("unusual activity", res["error"])
        self.assertGreaterEqual(acc.restricted_until - time.time(), 7000)
        self.assertEqual(acc.last_error, "HTTP 403 (account restricted)")
        self.assertIn("unusual activity", acc.last_error_detail)

        with io.open(acc.path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertGreater(saved.get("restrictedUntil", 0), time.time())
        reloaded = wb_accounts.Account(saved, path=acc.path)
        self.assertGreater(reloaded.restricted_until, time.time())

    # ---- 2. 限制期内不再发请求 ----
    def test_restricted_account_skips_daily_chat_without_upstream(self):
        acc = self.make_account()
        self.run_upstream(acc, [(403, RESTRICT_BODY)])
        upstream = RaisingUpstream([(200, b"{}")])
        real = wb_accounts.urlopen
        wb_accounts.urlopen = upstream
        try:
            self.assertFalse(acc.can_daily_chat())
            res = acc.daily_chat(web=False)
        finally:
            wb_accounts.urlopen = real
        self.assertEqual(upstream.calls, [])
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("skipped"))
        self.assertTrue(res.get("restricted"))

    # ---- 3. 不触发整池连坐暂停 ----
    def test_no_realm_hold_from_the_offline_leg(self):
        for uid in ("dc00000a", "dc00000b", "dc00000c"):
            acc = self.make_account(uid=uid)
            self.run_upstream(acc, [(403, RESTRICT_BODY)])
        self.assertEqual(P.restriction_hold_remaining("intl"), 0.0)
        self.assertEqual(P._RESTRICTION_EVENTS.get("intl", {}), {})

    # ---- 4. 429 / 14017：不限制，只记错误 ----
    def test_trial_not_activated_is_not_a_restriction(self):
        acc = self.make_account("dc00trial")
        _, res = self.run_upstream(acc, [(429, TRIAL_BODY)])
        self.assertFalse(res["ok"])
        self.assertFalse(res.get("restricted"))
        self.assertEqual(acc.restricted_until, 0.0)
        self.assertIn("14017", res["error"])
        self.assertIn("trial version", res["error"])
        self.assertLessEqual(len(res["error"]), 420)
        self.assertTrue(acc.can_daily_chat())

    # ---- 5. 内容审核 403 原样返回 ----
    def test_content_rejection_is_not_a_restriction(self):
        acc = self.make_account("dc00content")
        _, res = self.run_upstream(acc, [(403, CONTENT_BODY)])
        self.assertFalse(res["ok"])
        self.assertFalse(res.get("restricted"))
        self.assertEqual(acc.restricted_until, 0.0)
        self.assertIn("10001", res["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
