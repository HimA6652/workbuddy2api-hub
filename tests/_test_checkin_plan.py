"""签到候选快照 GET /tasks/checkin/plan 与 POST /accounts/reload（规范 §2.3）。

/checkin/plan 是给面板回答「这一轮到底会签几个号、剩下的为什么不动」的接口，所以
三件事必须钉住：

1. **口径与调度器一致**：会签的判据就是 wb_scheduler._run_cycle 那一串 ——
   realm=="cn"、task_active()、凭据可用、can_checkin()、非企业计费、
   未被账号级风控、整池没被 hold。少一样就会让面板以为「会签」而调度器跳过，
   或者反过来。
2. **reason 的判定顺序**：not_cn → api_key → enterprise → tasks_disabled →
   credential_expired → restricted → region_hold → already_today →
   checkin_paused → will_attempt。顺序错了，一个既过期又已签到的号会报成
   already_today，运维照 reason 去排障就会排错方向。
3. **严格只读**：不发上游请求、不写盘。签到窗口里刷这个接口不能烧掉账号额度，
   也不能改动任何状态。

/accounts/reload 钉的是另一件事：面板会话是硬门（API Key 不行），且账号池没实现
reload_if_changed() 时要如实回报 unsupported，而不是假装重载成功。

Run with: python tests/_test_checkin_plan.py
"""
import os
import re
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy
import wb_settings

TODAY = time.strftime("%Y-%m-%d")
NOW_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_UNSET = object()


class FakeAccount(object):
    """只实现 /tasks/checkin/plan 会碰到的那些属性/纯函数。

    can_checkin() 照抄 wb_accounts.Account.can_checkin() 的口径，这样「已签到」
    这类判定在测试里走的是真逻辑，而不是一个随便拨的布尔值。任何打上游的入口都
    直接炸掉：这个接口必须只读。
    """

    def __init__(self, uid, realm="cn", tasks_enabled=True, last_checkin=None,
                 is_api_key=False, enterprise=False, restricted_until=0.0,
                 access_token="tok", token_expired=False, nickname=None,
                 credential=_UNSET):
        self.uid = uid
        self.realm = realm
        self.tasks_enabled = tasks_enabled
        self.last_checkin = last_checkin
        self.is_api_key = is_api_key
        self.enterprise = enterprise
        self.restricted_until = restricted_until
        self.access_token = access_token
        self.token_expired = token_expired
        self.nickname = nickname
        self.upstream_calls = 0
        if credential is not _UNSET:
            # 模拟 impl-accounts 的 P1-3：方法在账号对象上，判定结果由账号自己给。
            self.credential_usable = lambda result=credential: bool(result)

    def task_active(self):
        return bool(self.tasks_enabled)

    def is_enterprise(self):
        return bool(self.enterprise)

    def uses_enterprise_billing(self):
        return bool(self.is_api_key or self.enterprise)

    def can_checkin(self):
        if self.realm != "cn":
            return False
        if self.uses_enterprise_billing():
            return False
        if not self.last_checkin:
            return True
        return not str(self.last_checkin).startswith(TODAY)

    def checkin(self, trigger="unknown"):
        self.upstream_calls += 1
        raise AssertionError("GET /tasks/checkin/plan 不许打上游")

    def _checkin_upstream(self):
        self.upstream_calls += 1
        raise AssertionError("GET /tasks/checkin/plan 不许打上游")


class FakePool(object):
    def __init__(self, accounts, reload_result=_UNSET):
        self.accounts = accounts
        if reload_result is not _UNSET:
            # 只有显式给了结果才装 reload_if_changed：没装的那个池子用来钉住
            # 「P3-2 未落地」的分支（真账号池的旧版本就是这样）。
            self.reload_result = reload_result
            self.reload_calls = 0
            self.reload_if_changed = self._reload_if_changed

    def _reload_if_changed(self):
        self.reload_calls += 1
        return self.reload_result

    def get(self, uid):
        for account in self.accounts:
            if account.uid == uid:
                return account
        return None

    def task_accounts(self, realm=None):
        return [a for a in self.accounts
                if (not realm or a.realm == realm) and a.task_active()]

    def list_public(self, realm=None):
        return []


class Handler(wb_proxy.Handler):
    """只覆写出口与鉴权，路由方法本身跑的是生产代码。"""

    panel_ok = True

    def __init__(self):
        self.captured = None

    def _json(self, code, obj):
        self.captured = (code, obj)

    def _error(self, code, message, err_type="server_error"):
        self.captured = (code, {"error": message})

    def _authorized(self):
        return True

    def _panel_ok(self):
        return Handler.panel_ok


class PlanRouteTests(unittest.TestCase):
    checkin_on = True

    def setUp(self):
        self.accounts = [
            FakeAccount("c-new", nickname="新号"),
            FakeAccount("c-done", last_checkin=TODAY + " 09:00:00"),
            FakeAccount("c-task", tasks_enabled=False),
            FakeAccount("c-exp", token_expired=True),
            FakeAccount("c-rest", restricted_until=time.time() + 3600),
            FakeAccount("c-key", is_api_key=True),
            FakeAccount("c-ent", enterprise=True),
            FakeAccount("i-1", realm="intl", nickname="国际号"),
        ]
        self.pool = FakePool(self.accounts)
        self.holds = {"cn": 0.0, "intl": 0.0}
        for target, value in (
            ("POOL", self.pool),
            ("log", lambda *a, **k: None),
            ("account_views", lambda realm=None: []),
            ("restriction_hold_remaining",
             lambda realm, now=None: self.holds.get(realm, 0.0)),
        ):
            patcher = mock.patch.object(wb_proxy, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(wb_settings, "checkin_enabled",
                                    lambda accounts_dir=None: self.checkin_on)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, Handler, "panel_ok", True)

    def call(self, query=None, handler=None):
        handler = handler or Handler()
        handler._get_tasks_checkin_plan(query if query is not None else {})
        self.assertIsNotNone(handler.captured, "路由没有输出任何响应")
        return handler.captured

    def reasons_of(self, body):
        return {row["uid"]: row["reason"] for row in body["accounts"]}

    def test_reason_table_and_summary(self):
        code, body = self.call()
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["realm"], "all")
        self.assertRegex(body["now"], NOW_FORMAT)
        self.assertEqual(self.reasons_of(body), {
            "c-new": "will_attempt",
            "c-done": "already_today",
            "c-task": "tasks_disabled",
            "c-exp": "credential_expired",
            "c-rest": "restricted",
            "c-key": "api_key",
            "c-ent": "enterprise",
            "i-1": "not_cn",
        })
        summary = body["summary"]
        self.assertEqual(summary["total"], 8)
        self.assertEqual(summary["will_attempt"], 1)
        self.assertEqual(summary["skipped"], 7)
        self.assertIs(summary["checkin_enabled"], True)
        # reasons 只统计被跳过的理由，会签的数量另有 will_attempt 一栏。
        self.assertNotIn("will_attempt", summary["reasons"])
        self.assertEqual(summary["reasons"], {
            "already_today": 1, "tasks_disabled": 1, "credential_expired": 1,
            "restricted": 1, "api_key": 1, "enterprise": 1, "not_cn": 1,
        })
        self.assertEqual(sum(summary["reasons"].values()), summary["skipped"])

    def test_row_shape(self):
        _code, body = self.call()
        for row in body["accounts"]:
            self.assertEqual(
                set(row),
                {"uid", "nickname", "realm", "will_attempt", "reason",
                 "lastCheckin"})
        done = [r for r in body["accounts"] if r["uid"] == "c-done"][0]
        self.assertFalse(done["will_attempt"])
        self.assertEqual(done["lastCheckin"], TODAY + " 09:00:00")
        self.assertEqual(done["realm"], "cn")

    def test_will_attempt_rows_come_first_then_uid(self):
        self.accounts.append(FakeAccount("c-b"))
        self.accounts.append(FakeAccount("c-a"))
        _code, body = self.call()
        uids = [row["uid"] for row in body["accounts"]]
        self.assertEqual(uids[:3], ["c-a", "c-b", "c-new"])
        self.assertEqual(
            [row["will_attempt"] for row in body["accounts"][:3]],
            [True, True, True])
        tail = [row["uid"] for row in body["accounts"][3:]]
        self.assertEqual(tail, sorted(tail))
        self.assertFalse(any(row["will_attempt"] for row in body["accounts"][3:]))

    def test_reason_order_puts_credential_before_already_today(self):
        # 既过期又已签到的号：必须先报 credential_expired，否则运维会以为「明天再说就行」。
        self.accounts.append(FakeAccount("c-exp-done",
                                         token_expired=True,
                                         last_checkin=TODAY + " 09:00:00"))
        # 既被风控又已签到：restricted 优先。
        self.accounts.append(FakeAccount("c-rest-done",
                                         restricted_until=time.time() + 60,
                                         last_checkin=TODAY + " 09:00:00"))
        _code, body = self.call()
        self.assertEqual(self.reasons_of(body)["c-exp-done"], "credential_expired")
        self.assertEqual(self.reasons_of(body)["c-rest-done"], "restricted")

    def test_region_hold_only_hits_its_own_realm(self):
        self.holds["cn"] = 120.0
        _code, body = self.call()
        self.assertEqual(self.reasons_of(body)["c-new"], "region_hold")
        self.assertEqual(body["summary"]["will_attempt"], 0)

    def test_checkin_paused_hits_every_would_be_checkin(self):
        self.checkin_on = False
        self.accounts.append(FakeAccount("c-b"))
        _code, body = self.call()
        reasons = self.reasons_of(body)
        self.assertEqual(reasons["c-new"], "checkin_paused")
        self.assertEqual(reasons["c-b"], "checkin_paused")
        self.assertEqual(reasons["c-done"], "already_today",
                         "已签到的号还是 already_today，暂停不该改写历史")
        self.assertIs(body["summary"]["checkin_enabled"], False)
        self.assertEqual(body["summary"]["will_attempt"], 0)
        self.assertEqual(body["summary"]["reasons"].get("checkin_paused"), 2)

    def test_realm_filter(self):
        _code, cn = self.call({"realm": ["cn"]})
        self.assertEqual(cn["realm"], "cn")
        self.assertEqual({row["realm"] for row in cn["accounts"]}, {"cn"})
        _code, intl = self.call({"realm": ["intl"]})
        self.assertEqual([row["uid"] for row in intl["accounts"]], ["i-1"])
        self.assertEqual(intl["accounts"][0]["reason"], "not_cn")

    def test_realm_defaults_to_all_for_missing_or_bogus_values(self):
        for query in ({}, {"realm": []}, {"realm": [""]},
                      {"realm": ["xx"]}, {"realm": ["all"]}):
            with self.subTest(query=query):
                _code, body = self.call(query)
                self.assertEqual(body["realm"], "all")
                self.assertEqual(body["summary"]["total"], 8)

    def test_credential_usable_hook_wins_over_the_fallback(self):
        # P1-3 落地后 credential_usable() 是唯一口径：即使 access_token 还在，
        # 它说不可用就是不可用；反过来它说可用时，token_expired 的近似判据要让位。
        self.accounts.append(FakeAccount("c-hook-off", credential=False))
        self.accounts.append(FakeAccount("c-hook-on", token_expired=True,
                                         credential=True))
        _code, body = self.call()
        reasons = self.reasons_of(body)
        self.assertEqual(reasons["c-hook-off"], "credential_expired")
        self.assertEqual(reasons["c-hook-on"], "will_attempt")
        self.assertEqual(body["summary"]["will_attempt"], 2)

    def test_hook_raising_is_treated_as_unusable(self):
        account = FakeAccount("c-hook-boom")
        account.credential_usable = mock.Mock(side_effect=RuntimeError("boom"))
        self.accounts.append(account)
        _code, body = self.call()
        self.assertEqual(self.reasons_of(body)["c-hook-boom"],
                         "credential_expired")

    def test_it_is_strictly_read_only(self):
        with mock.patch.object(wb_settings, "save",
                              side_effect=AssertionError("这个接口不许写盘")):
            code, body = self.call()
        self.assertEqual(code, 200)
        self.assertEqual([a for a in self.accounts if a.upstream_calls], [],
                         "这个接口不许向上游发任何请求")
        self.assertEqual(body["summary"]["total"], 8)

    def test_unauthorized_call_writes_nothing(self):
        class Unauthorized(Handler):
            def _authorized(self):
                return False

        handler = Unauthorized()
        handler._get_tasks_checkin_plan({})
        self.assertIsNone(handler.captured)

    def test_an_empty_pool_reports_zeroes(self):
        self.pool.accounts = []
        _code, body = self.call()
        self.assertEqual(body["summary"], {
            "total": 0, "will_attempt": 0, "skipped": 0, "reasons": {},
            "checkin_enabled": True})
        self.assertEqual(body["accounts"], [])

    def test_pool_none_is_tolerated(self):
        with mock.patch.object(wb_proxy, "POOL", None):
            _code, body = self.call()
        self.assertEqual(body["summary"]["total"], 0)


class ReloadRouteTests(unittest.TestCase):
    def setUp(self):
        self.view = [{"uid": "c-new", "nickname": "13800138000"}]
        for target, value in (
            ("log", lambda *a, **k: None),
            ("account_views", lambda realm=None: list(self.view)),
        ):
            patcher = mock.patch.object(wb_proxy, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, Handler, "panel_ok", True)

    def pool(self, accounts=None, reload_result=_UNSET):
        pool = FakePool(accounts if accounts is not None else [FakeAccount("c-new")],
                        reload_result)
        patcher = mock.patch.object(wb_proxy, "POOL", pool)
        patcher.start()
        self.addCleanup(patcher.stop)
        return pool

    def call(self, payload=None):
        handler = Handler()
        handler._route_accounts_reload(payload if payload is not None else {})
        self.assertIsNotNone(handler.captured)
        return handler.captured

    def test_reload_reports_the_pool_change(self):
        pool = self.pool(reload_result={"changed": True,
                                        "added": ["c-new2"],
                                        "removed": ["c-old"]})
        code, body = self.call({})
        self.assertEqual(code, 200)
        self.assertEqual(pool.reload_calls, 1)
        self.assertTrue(body["ok"])
        self.assertTrue(body["changed"])
        self.assertEqual(body["added"], ["c-new2"])
        self.assertEqual(body["removed"], ["c-old"])
        self.assertEqual(body["accounts"], 1)
        self.assertIn("accountsView", body)
        # 账号视图走面板脱敏：手机号形态的昵称不许原样出去。
        self.assertNotEqual(body["accountsView"][0]["nickname"], "13800138000")

    def test_a_bare_boolean_result_still_works(self):
        self.pool(reload_result=True)
        _code, body = self.call({})
        self.assertTrue(body["changed"])
        self.assertEqual(body["added"], [])
        self.assertEqual(body["removed"], [])

    def test_a_pool_without_reload_says_unsupported(self):
        pool = self.pool()
        code, body = self.call({})
        self.assertEqual(code, 200)
        self.assertIs(body["unsupported"], True)
        self.assertIs(body["changed"], False)
        self.assertEqual(body["accounts"], 1)
        self.assertFalse(hasattr(pool, "reload_calls"))

    def test_the_panel_session_is_required(self):
        self.pool(reload_result={"changed": True})
        Handler.panel_ok = False
        code, body = self.call({})
        self.assertEqual(code, 403)
        self.assertIn("error", body)

    def test_the_real_pool_contract_is_wired_through(self):
        """真账号池（impl-accounts 的 P3-2）跑一遍，钉住我这边读的字段名。

        FakePool 只能证明「拿到 dict 后我怎么拼响应」；字段名对不对得让真
        AccountPool 说。它首次调用只记签名不重载（调用方决定要不要 load()），
        目录变了才重载并给出 added/removed —— 这三条语义都写死在这里，免得
        对面改签名时我的路由静默退化成 unsupported。
        """
        import json
        import tempfile
        import wb_accounts

        with tempfile.TemporaryDirectory() as folder:
            def write(uid):
                path = os.path.join(folder, uid + ".json")
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump({"uid": uid, "accessToken": "tok", "realm": "cn"},
                              handle)

            write("c-alpha")
            pool = wb_accounts.AccountPool(folder)
            pool.load()
            patcher = mock.patch.object(wb_proxy, "POOL", pool)
            patcher.start()
            self.addCleanup(patcher.stop)
            # 这一条不验脱敏，只验 reload 语义：视图直接回 uid，避开额度汇总。
            view_patcher = mock.patch.object(
                wb_proxy, "account_views",
                lambda realm=None: [{"uid": a.uid} for a in list(pool.accounts)])
            view_patcher.start()
            self.addCleanup(view_patcher.stop)

            _code, first = self.call({})
            self.assertIs(first["changed"], False)   # 首次只记签名
            self.assertEqual(first["accounts"], 1)
            self.assertEqual([row["uid"] for row in first["accountsView"]],
                             ["c-alpha"])

            write("c-beta")
            _code, added = self.call({})
            self.assertIs(added["changed"], True)
            self.assertEqual(added["added"], ["c-beta"])
            self.assertEqual(added["removed"], [])
            self.assertEqual(added["accounts"], 2)

            os.remove(os.path.join(folder, "c-alpha.json"))
            _code, removed = self.call({})
            self.assertIs(removed["changed"], True)
            self.assertEqual(removed["added"], [])
            self.assertEqual(removed["removed"], ["c-alpha"])
            self.assertEqual([row["uid"] for row in removed["accountsView"]],
                             ["c-beta"])

    def test_it_is_reachable_through_the_accounts_dispatcher(self):
        pool = self.pool(reload_result={"changed": False})
        handler = Handler()
        handler._handle_accounts("/accounts/reload", {})
        self.assertEqual(pool.reload_calls, 1)
        self.assertEqual(handler.captured[0], 200)
        self.assertIs(handler.captured[1]["changed"], False)


if __name__ == "__main__":
    unittest.main()
