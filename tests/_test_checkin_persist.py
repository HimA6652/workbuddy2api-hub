"""签到状态落盘、凭证门槛与连续认证失败自动停用（规范 §2.2）。

钉住的五件事：

1. P1-3：credential_usable() 只认「有 access_token 且未被判定过期」，
   can_checkin() 必须过这道门槛；P2-1：public()["canCheckin"] 与
   can_checkin() 同源，不再各自写一遍 realm/企业号判断。
2. P2-2：already（200 回执与 HTTP 400 两条路径）只在 lastCheckin 不是今天
   时才写；checked 总是写。配套：already / checked 且 lastCheckinAt 是今天时
   can_checkin() 必须 False，否则每轮巡检都会再问一次上游；跨天自动恢复。
3. P3-3：lastCheckinState / lastCheckinAt / checkinAttempts 落盘、能读回、
   跨天归零；每次尝试都写时刻与分类，且写盘在状态判定之后只做一次。
4. P3-3：返回值里的 transient（网络异常 / 5xx / 429）与 auth_failed（401
   或认证文案）分类。
5. P2-3：连续认证失败 3 次自动停用（阈值 0 关闭、刷新成功清零），并且读设置
   与写盘都发生在 _throttle_lock 之外。

全程不打网络：wb_accounts.http_json 一律被替换。
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_settings

TODAY = time.strftime("%Y-%m-%d")


def _yesterday():
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))


def _stamp(day=None, hms="08:00:00"):
    return "%s %s" % (day or TODAY, hms)


def _account(directory, uid="checkinpersist", token="token", data=None):
    payload = {"uid": uid, "accessToken": token, "realm": "cn"}
    if data:
        payload.update(data)
    account = wb_accounts.Account(payload, path=os.path.join(directory, uid + ".json"))
    account.headers = lambda purpose=None: {}
    return account


def _http_error(code, body=""):
    return urllib.error.HTTPError("https://example.invalid/", code, "err", {},
                                  io.BytesIO(body.encode("utf-8")))


def _disk(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class _TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="checkin-persist-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name

    def attempt(self, account, payload=None, error=None):
        """跑一次 _checkin_upstream，http_json 用假回执 / 假异常。"""
        if error is not None:
            patcher = mock.patch.object(wb_accounts, "http_json", side_effect=error)
        else:
            patcher = mock.patch.object(wb_accounts, "http_json", return_value=payload)
        with patcher:
            return account._checkin_upstream()


class CredentialGateTests(_TempDirCase):
    """P1-3 + P2-1：凭证门槛与面板同源。"""

    def test_no_token_is_not_usable(self):
        account = _account(self.directory, token="")
        self.assertFalse(account.credential_usable())
        self.assertFalse(account.can_checkin())
        self.assertFalse(account.public()["canCheckin"])

    def test_expired_token_is_not_usable(self):
        account = _account(self.directory)
        self.assertTrue(account.credential_usable())
        account.token_expired = True
        self.assertFalse(account.credential_usable())
        self.assertFalse(account.can_checkin())
        self.assertFalse(account.public()["canCheckin"])

    def test_healthy_cn_account_can_checkin(self):
        account = _account(self.directory)
        self.assertTrue(account.credential_usable())
        self.assertTrue(account.can_checkin())
        self.assertTrue(account.public()["canCheckin"])

    def test_disabled_account_still_may_checkin(self):
        """凭据可用性只看凭证：enabled=False（停派单）不影响签到。"""
        account = _account(self.directory)
        account.enabled = False
        self.assertTrue(account.credential_usable())
        self.assertTrue(account.can_checkin())

    def test_public_can_checkin_follows_the_method(self):
        account = _account(self.directory)
        account.last_checkin = _stamp(TODAY, "09:00:00")
        self.assertFalse(account.can_checkin())
        self.assertFalse(account.public()["canCheckin"])
        account.last_checkin = None
        self.assertTrue(account.public()["canCheckin"])


class AlreadyWriteGateTests(_TempDirCase):
    """P2-2：already 不覆写今天的时刻，checked 总是覆写。"""

    def test_already_keeps_todays_stamp_on_200(self):
        account = _account(self.directory)
        account.last_checkin = _stamp(TODAY, "00:00:01")
        result = self.attempt(account, payload={"code": 10001, "msg": "今天已签到"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "already")
        self.assertEqual(account.last_checkin, _stamp(TODAY, "00:00:01"))
        self.assertEqual(account.last_checkin_state, "already")
        self.assertEqual(account.checkin_attempts, 1)

    def test_already_fills_an_old_stamp_on_200(self):
        account = _account(self.directory)
        account.last_checkin = _stamp(_yesterday(), "21:00:00")
        self.attempt(account, payload={"code": 10001, "msg": "今天已签到"})
        self.assertEqual(account.last_checkin, account.last_checkin_at)
        self.assertTrue(account.last_checkin.startswith(TODAY))

    def test_already_keeps_todays_stamp_on_http_400(self):
        account = _account(self.directory)
        account.last_checkin = _stamp(TODAY, "00:00:01")
        result = self.attempt(
            account, error=_http_error(400, '{"code": 10001, "msg": "今天已签到，请明天再来"}'))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "already")
        self.assertEqual(account.last_checkin, _stamp(TODAY, "00:00:01"))

    def test_already_fills_an_empty_stamp_on_http_400(self):
        account = _account(self.directory)
        self.attempt(account, error=_http_error(400, '{"code": 10001, "msg": "今天已签到"}'))
        self.assertTrue(str(account.last_checkin or "").startswith(TODAY))

    def test_checked_always_overwrites(self):
        account = _account(self.directory)
        account.last_checkin = _stamp(TODAY, "00:00:01")
        result = self.attempt(account, payload={"code": 0, "msg": "success"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "checked")
        self.assertNotEqual(account.last_checkin, _stamp(TODAY, "00:00:01"))
        self.assertTrue(account.last_checkin.startswith(TODAY))

    def test_pending_and_failed_never_write_last_checkin(self):
        for payload in ({"code": 10001, "msg": "请求处理中，请勿重复操作"},
                        {"code": 0, "msg": "签到失败，已签到天数不足 7 天"}):
            with self.subTest(msg=payload["msg"]):
                with tempfile.TemporaryDirectory(prefix="checkin-gate-") as directory:
                    account = _account(directory)
                    self.attempt(account, payload=payload)
                    self.assertFalse(account.last_checkin)
                    # 但状态与时刻照写：面板要看得出「今天试过、什么结果」。
                    self.assertIn(account.last_checkin_state, ("pending", "failed"))
                    self.assertTrue(str(account.last_checkin_at).startswith(TODAY))


class AlreadyTodayGateTests(_TempDirCase):
    """P2-2 的必要配套：今天已经问过上游就不必再问一次。"""

    def test_checked_today_blocks_the_next_round(self):
        account = _account(self.directory)
        account.last_checkin_state = "checked"
        account.last_checkin_at = _stamp(TODAY, "09:00:05")
        self.assertFalse(account.can_checkin())

    def test_already_today_with_old_last_checkin_blocks_the_next_round(self):
        account = _account(self.directory)
        account.last_checkin = _stamp(_yesterday(), "21:00:00")
        account.last_checkin_state = "already"
        account.last_checkin_at = _stamp(TODAY, "09:00:05")
        self.assertFalse(account.can_checkin())
        self.assertFalse(account.public()["canCheckin"])

    def test_yesterdays_stamp_restores_checkin(self):
        account = _account(self.directory)
        account.last_checkin = _stamp(_yesterday(), "21:00:00")
        account.last_checkin_state = "already"
        account.last_checkin_at = _stamp(_yesterday(), "21:00:05")
        self.assertTrue(account.can_checkin())

    def test_pending_today_still_retries(self):
        account = _account(self.directory)
        account.last_checkin_state = "pending"
        account.last_checkin_at = _stamp(TODAY, "09:00:05")
        self.assertTrue(account.can_checkin())


class PersistFieldTests(_TempDirCase):
    """P3-3：state / at / attempts 落盘、读回、跨天归零。"""

    def test_attempt_is_persisted_to_disk(self):
        account = _account(self.directory)
        self.attempt(account, payload={"code": 10001, "msg": "请求处理中，请勿重复操作"})
        row = _disk(account.path)
        self.assertEqual(row.get("lastCheckinState"), "pending")
        self.assertTrue(str(row.get("lastCheckinAt")).startswith(TODAY))
        self.assertEqual(row.get("checkinAttempts"), 1)

    def test_fields_round_trip_into_a_new_account(self):
        account = _account(self.directory)
        self.attempt(account, payload={"code": 0, "msg": "success"})
        reloaded = wb_accounts.Account(_disk(account.path), path=account.path)
        self.assertEqual(reloaded.last_checkin_state, "checked")
        self.assertEqual(reloaded.last_checkin_at, account.last_checkin_at)
        self.assertEqual(reloaded.checkin_attempts, 1)
        self.assertEqual(reloaded.last_checkin, account.last_checkin)

    def test_attempts_accumulate_within_the_day(self):
        account = _account(self.directory)
        for _ in range(3):
            self.attempt(account, payload={"code": 0, "msg": "签到失败，已签到天数不足 7 天"})
        self.assertEqual(account.checkin_attempts, 3)
        self.assertEqual(_disk(account.path).get("checkinAttempts"), 3)

    def test_attempts_reset_across_days_on_load(self):
        data = {"uid": "crossday", "accessToken": "token", "realm": "cn",
                "lastCheckinAt": _stamp(_yesterday(), "21:00:00"),
                "lastCheckinState": "failed", "checkinAttempts": 7}
        account = wb_accounts.Account(data, path=os.path.join(self.directory, "crossday.json"))
        self.assertEqual(account.checkin_attempts, 0)
        self.assertEqual(account.last_checkin_state, "failed")   # 状态本身留着，供面板显示

    def test_attempts_reset_across_days_on_next_attempt(self):
        account = _account(self.directory, data={
            "lastCheckinAt": _stamp(_yesterday(), "21:00:00"),
            "lastCheckinState": "failed", "checkinAttempts": 4})
        self.attempt(account, payload={"code": 0, "msg": "success"})
        self.assertEqual(account.checkin_attempts, 1)

    def test_attempts_survive_a_plain_save(self):
        account = _account(self.directory, data={"checkinAttempts": 2,
                                                 "lastCheckinAt": _stamp(TODAY, "07:00:00"),
                                                 "lastCheckinState": "failed"})
        account.save(self.directory)
        self.assertEqual(_disk(account.path).get("checkinAttempts"), 2)

    def test_junk_counts_fall_back_to_zero(self):
        for junk in (None, "", "abc", [], {}, -5):
            with self.subTest(junk=junk):
                data = {"uid": "junk", "accessToken": "t", "realm": "cn",
                        "lastCheckinAt": _stamp(TODAY, "07:00:00"),
                        "checkinAttempts": junk}
                account = wb_accounts.Account(data)
                self.assertEqual(account.checkin_attempts, 0)


class ClassificationTests(_TempDirCase):
    """P3-3：transient / auth_failed 分类。"""

    def test_success_is_neither(self):
        account = _account(self.directory)
        result = self.attempt(account, payload={"code": 0, "msg": "success"})
        self.assertFalse(result["transient"])
        self.assertFalse(result["auth_failed"])

    def test_http_500_is_transient(self):
        account = _account(self.directory)
        result = self.attempt(account, error=_http_error(500, '{"code": 500, "msg": "internal"}'))
        self.assertFalse(result["ok"])
        self.assertTrue(result["transient"])
        self.assertFalse(result["auth_failed"])

    def test_http_429_is_transient(self):
        account = _account(self.directory)
        result = self.attempt(account, error=_http_error(429, ""))
        self.assertTrue(result["transient"])
        self.assertFalse(result["auth_failed"])

    def test_network_error_is_transient(self):
        account = _account(self.directory)
        result = self.attempt(account, error=urllib.error.URLError("timed out"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["transient"])
        self.assertFalse(result["auth_failed"])
        # 没拿到回执也要记一次尝试：面板要知道「今天试过」。
        self.assertEqual(account.last_checkin_state, "failed")
        self.assertEqual(account.checkin_attempts, 1)

    def test_http_401_is_auth_failed(self):
        account = _account(self.directory)
        result = self.attempt(account, error=_http_error(401, '{"code": 401, "msg": "unauthorized"}'))
        self.assertFalse(result["ok"])
        self.assertTrue(result["auth_failed"])
        self.assertFalse(result["transient"])

    def test_auth_text_in_a_200_body_is_auth_failed(self):
        account = _account(self.directory)
        result = self.attempt(account, payload={"code": 401, "msg": "token expired, unauthorized"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["auth_failed"])
        self.assertFalse(result["transient"])

    def test_pending_is_neither(self):
        account = _account(self.directory)
        result = self.attempt(account, payload={"code": 10001, "msg": "请求处理中，请勿重复操作"})
        self.assertFalse(result["transient"])
        self.assertFalse(result["auth_failed"])
        self.assertTrue(result["state"] == "pending")


class AutoDisableTests(_TempDirCase):
    """P2-3：连续认证失败自动停用。"""

    AUTH_TEXT = "HTTP 401 Unauthorized: token expired"

    def test_three_auth_failures_disable_the_account(self):
        account = _account(self.directory)
        for _ in range(2):
            account.note_error(self.AUTH_TEXT)
        self.assertTrue(account.enabled)
        self.assertTrue(account.tasks_enabled)
        self.assertEqual(account.auth_fail_streak, 2)

        account.note_error(self.AUTH_TEXT)
        self.assertEqual(account.auth_fail_streak, 3)
        self.assertFalse(account.enabled)
        self.assertFalse(account.tasks_enabled)
        self.assertIn("连续 3 次认证失败，已自动停用", account.note)
        self.assertTrue(account.note.endswith("）"), account.note)
        # 停用要落盘：重启后仍然是停用状态。
        row = _disk(account.path)
        self.assertFalse(row["enabled"])
        self.assertFalse(row["tasksEnabled"])
        self.assertIn("已自动停用", row["note"])

    def test_non_auth_errors_never_count(self):
        account = _account(self.directory)
        for _ in range(6):
            account.note_error("HTTP 500 internal")
        self.assertEqual(account.auth_fail_streak, 0)
        self.assertTrue(account.enabled)
        self.assertTrue(account.tasks_enabled)

    def test_streak_is_never_double_counted(self):
        account = _account(self.directory)
        for _ in range(5):
            account.note_error(self.AUTH_TEXT)
        self.assertEqual(account.note.count("已自动停用"), 1)

    def test_threshold_zero_turns_it_off(self):
        account = _account(self.directory)
        with mock.patch.object(wb_settings, "auto_disable_auth_failures",
                               lambda directory, *a, **k: 0, create=True):
            for _ in range(5):
                account.note_error(self.AUTH_TEXT)
        self.assertEqual(account.auth_fail_streak, 5)
        self.assertTrue(account.enabled)
        self.assertTrue(account.tasks_enabled)
        self.assertEqual(account.note, "")

    def test_threshold_comes_from_settings(self):
        account = _account(self.directory)
        with mock.patch.object(wb_settings, "auto_disable_auth_failures",
                               lambda directory, *a, **k: 2, create=True):
            account.note_error(self.AUTH_TEXT)
            self.assertTrue(account.enabled)
            account.note_error(self.AUTH_TEXT)
        self.assertFalse(account.enabled)
        self.assertIn("连续 2 次认证失败", account.note)

    def test_settings_and_save_run_outside_the_throttle_lock(self):
        account = _account(self.directory)
        probes = []

        def getter(directory, *a, **k):
            probes.append(("settings", account._throttle_lock.locked()))
            return 3

        real_save = account.save

        def spy_save(directory):
            probes.append(("save", account._throttle_lock.locked()))
            return real_save(directory)

        account.save = spy_save
        with mock.patch.object(wb_settings, "auto_disable_auth_failures", getter, create=True):
            for _ in range(3):
                account.note_error(self.AUTH_TEXT)
        names = [name for name, _ in probes]
        self.assertEqual(names[0], "settings")
        self.assertEqual(names.count("save"), 1)
        self.assertTrue(all(not locked for _, locked in probes), probes)

    def test_successful_refresh_clears_the_streak(self):
        account = _account(self.directory, data={"refreshToken": "r-old"})
        account.note_error(self.AUTH_TEXT)
        account.note_error(self.AUTH_TEXT)
        self.assertEqual(account.auth_fail_streak, 2)

        payload = {"code": 0, "data": {"accessToken": "new-access", "refreshToken": "r-new"}}
        with mock.patch.object(wb_accounts, "http_json", return_value=payload):
            self.assertTrue(account.refresh())
        self.assertEqual(account.access_token, "new-access")
        self.assertEqual(account.auth_fail_streak, 0)
        self.assertFalse(account.token_expired)
        self.assertTrue(account.enabled)

    def test_note_success_clears_the_streak(self):
        account = _account(self.directory)
        account.note_error(self.AUTH_TEXT)
        account.note_success()
        self.assertEqual(account.auth_fail_streak, 0)
        self.assertFalse(account.token_expired)


if __name__ == "__main__":
    unittest.main()
