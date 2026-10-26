# -*- coding: utf-8 -*-
"""签到层优化（checkin-opt-spec v1）验收：/tasks/checkin/plan 与 /accounts/reload。

verifier 独立编写的**黑盒**套件：只从 HTTP 路由入口（真正的 do_GET / do_POST
分发）和公开谓词出发，不 mock 被测路由自身，不 import 实现里的私有名字。

    D:/programdata/pyenv/python-3.12.13/python.exe tests/_test_verify_opt.py

规范 §2.3 的两条契约：

1. GET /tasks/checkin/plan?realm=cn|intl|all（默认 all）只读、不碰上游，
   鉴权口径与 GET /tasks 相同（面板路由 + _authorized()），返回
   summary.{total,will_attempt,skipped,reasons} 与每账号
   {uid,nickname,realm,will_attempt,reason,lastCheckin}，
   排序 = will_attempt 优先再按 uid，判定口径与调度器一致：
   realm=="cn" and task_active() and credential_usable() and can_checkin()
   and not uses_enterprise_billing() and restricted_until<=now and 区域 hold==0。
2. POST /accounts/reload（面板会话）→
   {"ok":true,"changed":bool,"added":[...],"removed":[...],"accounts":N,"accountsView":[...]}。

代码未落地时整份套件 SKIP 并打印 SKIP 行（阶段 1 骨架），落地后必须全绿。
"""
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

# 必须在 import wb_proxy 之前把 ACCOUNTS_DIR / usage 指到临时目录
_TMP = isolated_data_dirs("wb-verify-opt-")

import wb_accounts  # noqa: E402
import wb_proxy  # noqa: E402
import wb_scheduler  # noqa: E402
import wb_settings  # noqa: E402

REASONS = frozenset((
    "already_today", "tasks_disabled", "credential_expired", "enterprise",
    "api_key", "restricted", "region_hold", "not_cn",
))
NOW_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _skip_missing_plan():
    """缺失的 plan 待实现件；非空则该组跳过并打印 SKIP（阶段 1 骨架）。"""
    missing = []
    if not any("checkin_plan" in name for name in dir(wb_proxy.Handler)):
        missing.append("wb_proxy.Handler 上的 /tasks/checkin/plan 路由")
    return missing


def _skip_missing_reload():
    """缺失的 reload 待实现件；两半各自独立落地，跳过要分开报。"""
    missing = []
    if not any("accounts_reload" in name or "reload_if_changed" in name
               for name in dir(wb_proxy.Handler)):
        missing.append("wb_proxy.Handler 上的 /accounts/reload 路由")
    try:
        import wb_accounts
        if not hasattr(wb_accounts.AccountPool, "reload_if_changed"):
            missing.append("wb_accounts.AccountPool.reload_if_changed")
    except Exception:
        missing.append("wb_accounts.AccountPool.reload_if_changed")
    return missing


class FakeAccount(object):
    """调度器谓词的每个输入一个独立开关：单条件隔离时判定无歧义。"""

    def __init__(self, uid, realm="cn", tasks=True, usable=True, checkin_ok=True,
                 enterprise=False, api_key=False, restricted_until=0.0,
                 last_checkin="", nickname=None):
        self.uid = uid
        self.nickname = nickname or ("昵称-" + uid)
        self.realm = realm
        self.platform = "web"
        self.tasks_enabled = tasks
        self.enabled = True
        self.product = "personal"
        self.enterprise_id = "ent-" + uid if enterprise else None
        self.is_api_key = bool(api_key)
        self.restricted_until = float(restricted_until)
        self.last_checkin = last_checkin
        self._usable = bool(usable)
        self._checkin_ok = bool(checkin_ok)
        self._enterprise = bool(enterprise)
        self.calls = []

    # --- 被验收的四个公开谓词（plan 允许调用，它们是纯判定）---
    def task_active(self):
        self.calls.append("task_active")
        return bool(self.tasks_enabled)

    def credential_usable(self):
        self.calls.append("credential_usable")
        return bool(self._usable)

    def can_checkin(self):
        self.calls.append("can_checkin")
        return bool(self._checkin_ok and self.realm == "cn")

    def is_enterprise(self):
        return bool(self._enterprise)

    def uses_enterprise_billing(self):
        self.calls.append("uses_enterprise_billing")
        return bool(self._enterprise or self.is_api_key)

    def public(self):
        return {"uid": self.uid, "nickname": self.nickname, "realm": self.realm,
                "lastCheckin": self.last_checkin}

    # --- 只读接口绝不许碰的东西 ---
    def checkin(self, *a, **k):
        raise AssertionError("plan/reload 是只读接口，不许触发签到上游请求")

    def daily_chat(self, *a, **k):
        raise AssertionError("plan/reload 是只读接口，不许触发打卡上游请求")

    def travel(self, *a, **k):
        raise AssertionError("plan 是只读接口，不许触发猫猫旅行")

    def refresh(self, *a, **k):
        raise AssertionError("plan 是只读接口，不许刷新 token")

    def report_activity(self, *a, **k):
        raise AssertionError("plan 是只读接口，不许上报活跃")


class FakePool(object):
    def __init__(self, accounts, reload_result=None):
        self.accounts = list(accounts)
        self.reload_result = reload_result or {"changed": False,
                                               "checked": len(accounts),
                                               "added": [], "removed": []}
        self.reload_calls = 0

    def get(self, uid):
        for account in self.accounts:
            if account.uid == uid:
                return account
        return None

    def task_accounts(self, realm=None):
        return [a for a in self.accounts
                if (not realm or a.realm == realm) and a.task_active()]

    def list_public(self, realm=None):
        return [a.public() for a in self.accounts
                if (not realm or realm == "all" or a.realm == realm)]

    def reload_if_changed(self):
        self.reload_calls += 1
        return dict(self.reload_result)


class CapturingHandler(wb_proxy.Handler):
    """只留下分发与抓取响应，不碰 socket。"""

    def __init__(self, path="/"):
        self.path = path
        self.headers = {}
        self.answers = []

    # 真实 do_GET/do_POST 会用到、但与规则无关的钩子
    def _apply_pinned_route(self):
        return True

    def _handle_expect_continue(self):
        return None

    def _discard_body(self):
        return None

    def _panel_ok(self):
        return True

    def _key_ok(self):
        return True

    def _authorized(self):
        return True

    def _payload_or_error(self, allow_list=False):
        return {}

    # 响应抓取：_json_panel 最终也会落到 _json
    def _json(self, code, obj):
        self.answers.append((code, obj))
        return code

    def _error(self, code, message, err_type="server_error"):
        self.answers.append((code, {"error": message}))
        return code

    # 结果读取
    def last(self):
        assert self.answers, "路由没有产生任何响应"
        return self.answers[-1]

    def status(self):
        return self.last()[0]

    def body(self):
        return self.last()[1]


def _spec_predicate(account, now, region_hold):
    """规范 §2.3 的判定口径，独立重写一份用于对拍（不与实现共享代码）。"""
    return bool(
        account.realm == "cn"
        and account.task_active()
        and account.credential_usable()
        and account.can_checkin()
        and not account.uses_enterprise_billing()
        and float(account.restricted_until or 0.0) <= now
        and not region_hold
    )


class _PlanBase(unittest.TestCase):
    """plan 与 reload 各自独立落地，所以跳过检查按半套做。"""

    missing_probe = staticmethod(_skip_missing_plan)

    @classmethod
    def setUpClass(cls):
        missing = cls.missing_probe()
        if missing:
            print("SKIP: 待实现 -> " + "; ".join(missing))
            raise unittest.SkipTest("待实现: " + "; ".join(missing))

    def setUp(self):
        self._patches = []

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def patch(self, target, value, **kwargs):
        patcher = mock.patch.object(target[0], target[1], value, **kwargs)
        patcher.start()
        self._patches.append(patcher)
        return patcher

    def patch_pool(self, accounts, reload_result=None):
        pool = FakePool(accounts, reload_result=reload_result)
        self.patch((wb_proxy, "POOL"), pool)
        # 区域整池暂停：直接改真实字典，任何读它的辅助函数都会看到
        self.patch((wb_proxy, "_RESTRICTION_HOLD"), {}, create=True)
        self.reload_pool = pool
        return pool

    def get_plan(self, query="realm=all"):
        handler = CapturingHandler(
            "/tasks/checkin/plan" + (("?" + query) if query else ""))
        handler.do_GET()
        return handler

    def get_plan_body(self, query="realm=all"):
        handler = self.get_plan(query)
        self.assertEqual(handler.status(), 200,
                         "GET /tasks/checkin/plan 应回 200，实际 %s: %s"
                         % (handler.status(), handler.body()))
        return handler.body()


class CheckinPlanShapeTests(_PlanBase):
    """§2.3 GET /tasks/checkin/plan：形状、鉴权、只读。"""

    def test_route_reaches_200_and_documented_shape(self):
        self.patch_pool([FakeAccount("u1"), FakeAccount("u2", checkin_ok=False)])
        body = self.get_plan_body()
        self.assertTrue(body.get("ok"))
        self.assertRegex(str(body.get("now")), NOW_RE)
        self.assertEqual(body.get("realm"), "all")
        summary = body.get("summary") or {}
        for key in ("total", "will_attempt", "skipped", "reasons"):
            self.assertIn(key, summary, "summary 缺字段 %s" % key)
        self.assertEqual(summary["total"], 2)
        self.assertIsInstance(summary["reasons"], dict)
        rows = body.get("accounts") or []
        self.assertEqual(len(rows), 2)
        for row in rows:
            for key in ("uid", "nickname", "realm", "will_attempt", "reason",
                        "lastCheckin"):
                self.assertIn(key, row, "账号行缺字段 %s: %r" % (key, row))
            self.assertIsInstance(row["will_attempt"], bool)
            self.assertIsInstance(row["reason"], str)
            self.assertIsInstance(row["realm"], str)

    def test_panel_session_required_like_tasks(self):
        self.patch_pool([FakeAccount("u1")])
        handler = CapturingHandler("/tasks/checkin/plan?realm=all")
        handler._panel_ok = lambda: False
        handler.do_GET()
        self.assertEqual(handler.status(), 401,
                         "无面板会话应 401（与 /tasks 同一道门），实际 %s"
                         % handler.status())

    def test_api_key_alone_is_not_enough(self):
        """/tasks 是面板路由：光有 API key 不够，这也是 plan 的口径。"""
        self.patch_pool([FakeAccount("u1")])
        handler = CapturingHandler("/tasks/checkin/plan?realm=all")
        handler._panel_ok = lambda: False
        handler._key_ok = lambda: True
        handler.do_GET()
        self.assertEqual(handler.status(), 401)

    def test_realm_filter_cn_and_intl(self):
        self.patch_pool([FakeAccount("cn1"), FakeAccount("cn2"),
                         FakeAccount("i1", realm="intl")])
        cn = self.get_plan_body("realm=cn")
        self.assertEqual(cn["realm"], "cn")
        self.assertEqual({r["uid"] for r in cn["accounts"]}, {"cn1", "cn2"})
        self.assertEqual(cn["summary"]["total"], 2)
        intl = self.get_plan_body("realm=intl")
        self.assertEqual({r["uid"] for r in intl["accounts"]}, {"i1"})
        self.assertEqual(intl["summary"]["total"], 1)

    def test_default_realm_is_all(self):
        self.patch_pool([FakeAccount("cn1"), FakeAccount("i1", realm="intl")])
        body = self.get_plan_body("")
        self.assertEqual(body["realm"], "all")
        self.assertEqual(body["summary"]["total"], 2)

    def test_reason_vocabulary_is_closed(self):
        accounts = [
            FakeAccount("ok1"),
            FakeAccount("cn-done", checkin_ok=False),
            FakeAccount("cn-off", tasks=False),
            FakeAccount("cn-exp", usable=False),
            FakeAccount("cn-ent", enterprise=True),
            FakeAccount("cn-key", api_key=True),
            FakeAccount("cn-restricted", restricted_until=time.time() + 120),
            FakeAccount("i1", realm="intl"),
        ]
        self.patch_pool(accounts)
        body = self.get_plan_body()
        for row in body["accounts"]:
            if row["will_attempt"]:
                # 会签那一行的 reason 规范没规定：只要求它不是一个「跳过理由」
                self.assertNotIn(row["reason"], REASONS,
                                 "will_attempt 行不该带跳过理由: %r" % (row,))
                continue
            self.assertIn(row["reason"], REASONS, "reason 越界: %r" % (row,))
        self.assertTrue(set(body["summary"]["reasons"]) <= set(REASONS),
                        "summary.reasons 出现未定义键: %r"
                        % body["summary"]["reasons"])

    def test_global_checkin_switch_is_respected(self):
        """P0-3 的总闸关掉时，候选必须是 0（否则面板会骗人说这轮会签）。"""
        import wb_settings
        self.patch_pool([FakeAccount("u1"), FakeAccount("u2")])
        wb_settings.set_checkin_enabled(wb_proxy.ACCOUNTS_DIR, False)
        try:
            body = self.get_plan_body()
        finally:
            wb_settings.set_checkin_enabled(wb_proxy.ACCOUNTS_DIR, True)
        self.assertEqual(body["summary"]["will_attempt"], 0,
                         "checkin_enabled=False 时不应有候选: %r" % (body["summary"],))
        self.assertFalse(body["summary"].get("checkin_enabled", False))

    def test_no_upstream_or_mutating_calls(self):
        accounts = [FakeAccount("u%d" % i, checkin_ok=bool(i % 2))
                    for i in range(6)]
        self.patch_pool(accounts)
        before = [(a.uid, a.last_checkin, a.tasks_enabled) for a in accounts]
        self.get_plan_body()
        self.assertEqual(before, [(a.uid, a.last_checkin, a.tasks_enabled)
                                  for a in accounts],
                         "plan 不得改写账号状态")
        for account in accounts:
            self.assertNotIn("checkin", account.calls)
            self.assertNotIn("refresh", account.calls)


class CheckinPlanReasonTableTests(_PlanBase):
    """单个条件隔离，逐条核对 reason 与 will_attempt。"""

    def _one(self, account, hold=0.0):
        self.patch((wb_proxy, "POOL"), FakePool([account]))
        self.patch((wb_proxy, "_RESTRICTION_HOLD"),
                   {"cn": time.time() + hold} if hold else {})
        with mock.patch.object(wb_proxy, "restriction_hold_remaining",
                               side_effect=lambda realm, now=None: hold
                               if realm == "cn" else 0.0, create=True):
            handler = CapturingHandler("/tasks/checkin/plan")
            handler.do_GET()
        self.assertEqual(handler.status(), 200,
                         "plan 应 200，实际 %s: %s"
                         % (handler.status(), handler.body()))
        body = handler.body()
        rows = body["accounts"]
        self.assertEqual(len(rows), 1, "期望 1 行，实际 %r" % (rows,))
        return rows[0], body["summary"]

    def test_clean_cn_account_will_attempt(self):
        row, summary = self._one(FakeAccount("u1"))
        self.assertTrue(row["will_attempt"], "干净 cn 号必须 will_attempt: %r" % row)
        self.assertEqual(summary["will_attempt"], 1)
        self.assertEqual(summary["skipped"], 0)

    def test_already_today(self):
        row, summary = self._one(FakeAccount("u1", checkin_ok=False,
                                             last_checkin="2026-10-26 09:00:00"))
        self.assertFalse(row["will_attempt"])
        self.assertEqual(row["reason"], "already_today")
        self.assertEqual(summary["reasons"].get("already_today"), 1)

    def test_tasks_disabled(self):
        row, _ = self._one(FakeAccount("u1", tasks=False))
        self.assertFalse(row["will_attempt"])
        self.assertEqual(row["reason"], "tasks_disabled")

    def test_credential_expired(self):
        row, _ = self._one(FakeAccount("u1", usable=False))
        self.assertFalse(row["will_attempt"])
        self.assertEqual(row["reason"], "credential_expired")

    def test_enterprise(self):
        row, _ = self._one(FakeAccount("u1", enterprise=True))
        self.assertFalse(row["will_attempt"])
        self.assertEqual(row["reason"], "enterprise")

    def test_static_api_key(self):
        row, _ = self._one(FakeAccount("u1", api_key=True))
        self.assertFalse(row["will_attempt"])
        self.assertEqual(row["reason"], "api_key",
                         "静态 Key 号应与 enterprise 区分开: %r" % row)

    def test_restricted(self):
        row, _ = self._one(FakeAccount("u1", restricted_until=time.time() + 120))
        self.assertFalse(row["will_attempt"])
        self.assertEqual(row["reason"], "restricted")

    def test_region_hold(self):
        row, summary = self._one(FakeAccount("u1"), hold=90.0)
        self.assertFalse(row["will_attempt"])
        self.assertEqual(row["reason"], "region_hold")
        self.assertEqual(summary["reasons"].get("region_hold"), 1)

    def test_not_cn(self):
        row, _ = self._one(FakeAccount("i1", realm="intl"))
        self.assertFalse(row["will_attempt"])
        self.assertEqual(row["reason"], "not_cn")

    def test_intl_stays_not_cn_under_cn_region_hold(self):
        """区域 hold 只锁 cn 池：国际版仍按 not_cn 判定，不被 cn hold 吞掉。"""
        self.patch((wb_proxy, "_RESTRICTION_HOLD"), {"cn": time.time() + 90})
        pool = FakePool([FakeAccount("cn1"), FakeAccount("i1", realm="intl")])
        with mock.patch.object(wb_proxy, "restriction_hold_remaining",
                               side_effect=lambda realm, now=None: 90.0
                               if realm == "cn" else 0.0, create=True), \
             mock.patch.object(wb_proxy, "POOL", pool):
            handler = CapturingHandler("/tasks/checkin/plan")
            handler.do_GET()
        self.assertEqual(handler.status(), 200)
        by_uid = {row["uid"]: row for row in handler.body()["accounts"]}
        self.assertEqual(by_uid["cn1"]["reason"], "region_hold")
        self.assertEqual(by_uid["i1"]["reason"], "not_cn")


class CheckinPlanConsistencyTests(_PlanBase):
    """与调度器口径对拍 + 排序 + 汇总自洽。"""

    def _mixed(self, seed=20261026, size=24):
        rnd = random.Random(seed)
        accounts = []
        for index in range(size):
            realm = "cn" if index % 4 else "intl"
            accounts.append(FakeAccount(
                "acc%02d" % index,
                realm=realm,
                tasks=rnd.random() > 0.25,
                usable=rnd.random() > 0.2,
                checkin_ok=rnd.random() > 0.4,
                enterprise=(index % 7 == 3),
                api_key=(index % 11 == 5),
                restricted_until=(time.time() + 60 if index % 9 == 1 else 0.0),
                last_checkin=("2026-10-26 09:00:00" if index % 5 == 0 else ""),
            ))
        return accounts

    def test_will_attempt_matches_spec_predicate(self):
        accounts = self._mixed()
        self.patch_pool(accounts)
        with mock.patch.object(wb_proxy, "restriction_hold_remaining",
                               return_value=0.0, create=True):
            body = self.get_plan_body()
        now = time.time()
        expected = {a.uid for a in accounts if _spec_predicate(a, now, 0.0)}
        actual = {row["uid"] for row in body["accounts"] if row["will_attempt"]}
        self.assertEqual(expected, actual,
                         "plan 判定与规范口径不一致：漏 %s / 多 %s"
                         % (sorted(expected - actual), sorted(actual - expected)))

    def test_summary_is_self_consistent(self):
        accounts = self._mixed()
        self.patch_pool(accounts)
        with mock.patch.object(wb_proxy, "restriction_hold_remaining",
                               return_value=0.0, create=True):
            body = self.get_plan_body()
        summary = body["summary"]
        rows = body["accounts"]
        self.assertEqual(summary["total"], len(rows))
        self.assertEqual(summary["total"], len(accounts))
        self.assertEqual(summary["will_attempt"],
                         sum(1 for r in rows if r["will_attempt"]))
        self.assertEqual(summary["skipped"],
                         summary["total"] - summary["will_attempt"])
        self.assertEqual(sum(summary["reasons"].values()), summary["skipped"],
                         "reasons 分布之和必须等于 skipped: %r" % (summary,))
        self.assertTrue(all(count > 0 for count in summary["reasons"].values()),
                        "reasons 里不该留 0 计数: %r" % (summary,))

    def test_order_will_attempt_first_then_uid(self):
        accounts = [
            FakeAccount("b-skip", checkin_ok=False),
            FakeAccount("z-go"),
            FakeAccount("a-go"),
            FakeAccount("a-skip", tasks=False),
        ]
        self.patch_pool(accounts)
        body = self.get_plan_body()
        order = [(row["will_attempt"], row["uid"]) for row in body["accounts"]]
        self.assertEqual(order, [(True, "a-go"), (True, "z-go"),
                                 (False, "a-skip"), (False, "b-skip")],
                         "排序应为 will_attempt 优先再按 uid: %r" % (order,))


class AccountsReloadRouteTests(_PlanBase):
    """§2.3 POST /accounts/reload。"""

    @staticmethod
    def missing_probe():
        return _skip_missing_plan() + _skip_missing_reload()

    def post_reload(self, panel_ok=True, reload_result=None, accounts=None):
        accounts = (accounts if accounts is not None
                    else [FakeAccount("u%d" % i) for i in range(5)])
        pool = self.patch_pool(accounts, reload_result=reload_result)
        views = [{"uid": a.uid, "nickname": a.nickname} for a in accounts]
        self.patch((wb_proxy, "account_views"), lambda realm=None: list(views))
        handler = CapturingHandler("/accounts/reload")
        handler._panel_ok = lambda: bool(panel_ok)
        handler.do_POST()
        return handler, pool, views

    def test_reload_shape_and_delegation(self):
        result = {"changed": True, "checked": 5, "added": ["u9"], "removed": ["u0"]}
        handler, pool, views = self.post_reload(reload_result=result)
        self.assertEqual(handler.status(), 200,
                         "POST /accounts/reload 应 200，实际 %s: %s"
                         % (handler.status(), handler.body()))
        body = handler.body()
        self.assertIs(body.get("ok"), True)
        self.assertIs(body.get("changed"), True)
        self.assertEqual(body.get("added"), ["u9"])
        self.assertEqual(body.get("removed"), ["u0"])
        self.assertEqual(body.get("accounts"), 5)
        self.assertEqual([row.get("uid") for row in body.get("accountsView") or []],
                         [row["uid"] for row in views])
        self.assertEqual(pool.reload_calls, 1,
                         "路由必须调用 pool.reload_if_changed()")

    def test_reload_unchanged_case(self):
        result = {"changed": False, "checked": 5, "added": [], "removed": []}
        handler, _pool, _views = self.post_reload(reload_result=result)
        body = handler.body()
        self.assertIs(body.get("changed"), False)
        self.assertEqual(body.get("added"), [])
        self.assertEqual(body.get("removed"), [])

    def test_reload_requires_panel_session(self):
        handler, pool, _views = self.post_reload(panel_ok=False)
        self.assertIn(handler.status(), (401, 403),
                      "无面板会话必须被挡（401/403），实际 %s" % handler.status())
        self.assertEqual(pool.reload_calls, 0, "被挡的请求不许触发重载")

    def test_reload_registered_in_post_dispatch(self):
        """真分发：路径必须真的挂在 do_POST 链上，不是孤立方法。"""
        handler, _pool, _views = self.post_reload()
        self.assertNotEqual(handler.status(), 404,
                            "POST /accounts/reload 没被分发（404），路由未注册")


# ======================================================================
# 阶段 2：调度器 / 账号层验收
#    P1-1 抖动只加在真要发请求之前；P1-2 并发上限 / 错峰 / 截止时间；
#    P1-3 credential_usable 门槛；P2-2 already 不覆写当天时刻；
#    P2-3 连续认证失败自动停用（落盘在锁外）；P2-4 每轮一行汇总落盘；
#    P3-1 启动补跑；P3-2 巡检前 reload_if_changed；P3-3 瞬态失败退避重试。
# 全部从调度器/账号的公开入口（_run_cycle / _checkin_with_retry / Account.checkin）
# 出发，只替身「上游 HTTP」与「面板日志」这两条边界外的依赖。
# ======================================================================

_REAL_SLEEP = time.sleep


def _now_hour():
    return time.localtime().tm_hour


def _hours_before_now():
    """今天已经过去的一个整点；0 点用 -1 兜底（只用于「有没有错过时段」判断）。"""
    hour = _now_hour()
    return [hour - 1 if hour > 0 else -1]


def _skip_missing_scheduler():
    need = ("_plan_cycle_tasks", "_run_cn_task_batch", "_run_cn_tasks",
            "_checkin_with_retry", "_log_cycle_summary", "_reload_accounts_if_changed")
    return ["wb_scheduler.Scheduler." + name for name in need
            if not hasattr(wb_scheduler.Scheduler, name)]


class _FakeProxyModule(object):
    """wb_scheduler 只碰这两处 proxy 接口：面板日志与区域风控暂停。"""

    def add_log_entry(self, *args, **kwargs):
        return None

    def restriction_hold_remaining(self, realm, now=None):
        return 0.0


class _SchedAccount(object):
    """调度器跑道用的账号替身：只实现 _plan_cycle_tasks / _run_cn_tasks 会读的谓词。"""

    def __init__(self, uid, realm="cn", checkin_ok=True, tasks=True,
                 report_ok=False, daily_chat_ok=False, checkin_results=None,
                 on_checkin=None, expires_at=None):
        self.uid = uid
        self.realm = realm
        self.access_token = "token-" + uid
        self.token_expired = False
        self.expires_at = (time.time() + 30 * 86400) if expires_at is None else expires_at
        self.restricted_until = 0.0
        self.last_error = ""
        self.last_checkin = ""
        self.tasks_enabled = bool(tasks)
        self._checkin_ok = bool(tasks) and bool(checkin_ok)
        self._tasks = bool(tasks)
        self._report_ok = bool(report_ok)
        self._daily_chat_ok = bool(daily_chat_ok)
        self._results = list(checkin_results or [])
        self._on_checkin = on_checkin
        self.calls = []

    def task_active(self):
        return self._tasks

    def credential_usable(self):
        return bool(self.access_token) and not self.token_expired

    def can_checkin(self):
        return self._checkin_ok and self.realm == "cn"

    def can_report_activity(self):
        return self._report_ok

    def can_daily_chat(self):
        return self._daily_chat_ok

    def refresh(self):
        self.calls.append("refresh")
        return {"ok": True, "msg": "token 已刷新"}

    def checkin(self, trigger=""):
        self.calls.append("checkin")
        if self._on_checkin is not None:
            self._on_checkin(self)
        if self._results:
            return dict(self._results.pop(0))
        return {"ok": True, "msg": "签到成功", "transient": False}

    def report_activity(self):
        self.calls.append("report")
        return {"ok": True, "msg": "活跃上报成功"}

    def daily_chat(self):
        self.calls.append("daily_chat")
        return {"ok": True, "msg": "每日活跃完成"}


class _SchedPool(object):
    def __init__(self, directory, accounts):
        self.dir = directory
        self.accounts = list(accounts)
        self.reload_calls = 0
        self.reload_result = {"changed": False, "checked": len(self.accounts),
                             "added": [], "removed": []}

    def reload_if_changed(self):
        self.reload_calls += 1
        return dict(self.reload_result)


class _SchedulerCase(unittest.TestCase):
    """调度器跑道基类：替身上游、记录所有 time.sleep（>=0.9s 的不真睡）。"""

    missing_probe = staticmethod(_skip_missing_scheduler)

    def setUp(self):
        missing = self.missing_probe()
        if missing:
            print("SKIP: 待实现 -> %s" % "，".join(missing))
            raise unittest.SkipTest("待实现: %s" % "，".join(missing))
        self._tmp = tempfile.mkdtemp(prefix="wb-verify-sched-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.dir = os.path.join(self._tmp, "accounts")
        os.makedirs(self.dir, exist_ok=True)
        self.sleeps = []
        self.travel_calls = []
        self.night_calls = []
        for target, name, bucket in (
                (wb_scheduler, "do_cat_travel", self.travel_calls),
                (wb_scheduler.wb_tasks, "run_streak_bonus", self.travel_calls),
                (wb_scheduler.wb_tasks, "run_night_growth", self.night_calls)):
            patcher = mock.patch.object(
                target, name,
                (lambda store: lambda acc: store.append(acc) or {"logs": []})(bucket))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(wb_scheduler, "_proxy_module",
                                    lambda: _FakeProxyModule())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(wb_scheduler.time, "sleep", self._record_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record_sleep(self, seconds=0.0):
        self.sleeps.append(float(seconds))
        if float(seconds) < 0.9:      # 错峰/短退避：真睡但截断，保住并发时序
            _REAL_SLEEP(min(float(seconds), 0.05))

    def make_scheduler(self, accounts, hours=None, travel_hours=(),
                       cat_hours=(), keepalive_hours=()):
        self.pool = _SchedPool(self.dir, accounts)
        scheduler = wb_scheduler.Scheduler(self.pool)
        scheduler.logs = []
        scheduler.checkin_hours = list(hours if hours is not None else [_now_hour()])
        scheduler.travel_hours = list(travel_hours)
        scheduler.cat_hours = list(cat_hours)
        scheduler.keepalive_hours = list(keepalive_hours)
        scheduler.daily_chat_times = []
        return scheduler

    def log_text(self, scheduler):
        return "\n".join(scheduler.logs)


class SchedulerIdleCostTests(_SchedulerCase):
    """P1-1：不干活就不许等。"""

    def test_idle_cycle_never_sleeps(self):
        accounts = [_SchedAccount("idle%03d" % i, tasks=False, checkin_ok=False)
                    for i in range(120)]
        scheduler = self.make_scheduler(accounts)
        scheduler._run_cycle("验收-空转")
        self.assertEqual(self.sleeps, [],
                         "空转一轮出现等待：%r" % (self.sleeps[:10],))
        for account in accounts:
            self.assertEqual(account.calls, [], "空转账号被发了请求：%s" % account.uid)
        text = self.log_text(scheduler)
        self.assertIn("巡检汇总（", text)
        self.assertIn("候选 0", text)
        self.assertIn("tasks_disabled=120", text)

    def test_only_real_work_pays_the_stagger(self):
        workers = 3
        accounts = ([_SchedAccount("work%03d" % i) for i in range(workers)]
                    + [_SchedAccount("skip%03d" % i, tasks=False, checkin_ok=False)
                       for i in range(60)])
        scheduler = self.make_scheduler(accounts)
        scheduler._run_cycle("验收-少量干活")
        total = sum(self.sleeps)
        self.assertLess(total, 30.0,
                        "只有 %d 个账号要干活，等待却累计 %.1fs" % (workers, total))
        for account in accounts[workers:]:
            self.assertEqual(account.calls, [], "跳过账号被发了请求：%s" % account.uid)
        self.assertEqual(sum(1 for a in accounts[:workers] if a.calls), workers)


class SchedulerBatchTests(_SchedulerCase):
    """P1-2：并发上限、错峰、串行回落、批次截止时间。"""

    def _concurrency_probe(self, accounts, limit, work=0.06):
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def on_checkin(_account):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            _REAL_SLEEP(work)
            with lock:
                state["active"] -= 1

        for account in accounts:
            account._on_checkin = on_checkin
        wb_settings.set_tasks_concurrency(self.dir, limit)
        return state

    def test_concurrency_limit_is_respected(self):
        accounts = [_SchedAccount("cc%02d" % i) for i in range(6)]
        state = self._concurrency_probe(accounts, 3)
        scheduler = self.make_scheduler(accounts)
        scheduler._run_cycle("验收-并发")
        self.assertLessEqual(state["peak"], 3,
                             "并发峰值 %d 超过设置 3" % state["peak"])
        self.assertGreaterEqual(state["peak"], 2,
                                "并发峰值只有 %d，批量化没生效" % state["peak"])
        self.assertTrue(any(a.calls.count("checkin") == 1 for a in accounts))

    def test_stagger_is_short_and_present(self):
        accounts = [_SchedAccount("st%02d" % i) for i in range(6)]
        self._concurrency_probe(accounts, 3, work=0.02)
        scheduler = self.make_scheduler(accounts)
        scheduler._run_cycle("验收-错峰")
        stagger = [d for d in self.sleeps if 0.2 <= d <= 0.65]
        self.assertTrue(stagger, "并发批次没有错峰等待：%r" % (self.sleeps,))
        # 实现把逐个提交的错峰写成 worker 内的 min(index*U(0.25,0.6), 2.0)：
        # 2.0 是它的上限（第 4 个以后的账号都会停在这里），不是旧的串行抖动，
        # 所以这里卡的是「不许出现 (2.0, 5.0] 区间」——旧串行 uniform(2.0,5.0)
        # 几乎必然落在这个开区间里。
        self.assertFalse([d for d in self.sleeps if 2.0 + 1e-9 < d <= 5.0],
                         "并发批次里出现了旧的 2~5s 串行抖动：%r" % (self.sleeps,))
        self.assertLessEqual(max(self.sleeps), 2.0 + 1e-9,
                             "错峰上限被突破：%r" % (self.sleeps,))

    def test_concurrency_one_falls_back_to_serial(self):
        accounts = [_SchedAccount("s%02d" % i) for i in range(3)]
        state = self._concurrency_probe(accounts, 1, work=0.01)
        scheduler = self.make_scheduler(accounts)
        scheduler._run_cycle("验收-串行回落")
        self.assertEqual(state["peak"], 1, "concurrency=1 仍在并发跑：%d" % state["peak"])
        serial = [d for d in self.sleeps if 2.0 <= d <= 5.0]
        self.assertGreaterEqual(len(serial), 2,
                                "concurrency=1 没有保留旧的 2~5s 抖动：%r" % (self.sleeps,))

    def test_batch_deadline_stops_the_batch(self):
        accounts = [_SchedAccount("dl%02d" % i) for i in range(12)]
        self._concurrency_probe(accounts, 2, work=0.5)
        wb_settings.set_tasks_budget(self.dir, 1)
        wb_settings.set_checkin_retries(self.dir, 0)
        scheduler = self.make_scheduler(accounts)
        started = time.time()
        scheduler._run_cycle("验收-截止时间")
        elapsed = time.time() - started
        self.assertLess(elapsed, 8.0,
                        "批次截止 1s 却在 %.1fs 后才收手" % elapsed)
        self.assertIn("截止时间", self.log_text(scheduler))
        done = sum(1 for a in accounts if a.calls)
        self.assertLess(done, len(accounts), "12 个账号全部跑完，截止时间没生效")


class SchedulerCatchupTests(_SchedulerCase):
    """P3-1：启动补跑只在启动首轮、且只在今天已过时段时补一轮。"""

    def test_startup_catchup_runs_checkin(self):
        accounts = [_SchedAccount("cu%02d" % i) for i in range(2)]
        wb_settings.set_checkin_startup_catchup(self.dir, True)
        scheduler = self.make_scheduler(accounts, hours=_hours_before_now())
        scheduler._run_cycle("验收-启动补跑", catchup_tasks=True)
        self.assertEqual([a.uid for a in accounts if "checkin" in a.calls],
                         [a.uid for a in accounts], "启动补跑没签到")
        self.assertIn("启动补跑", self.log_text(scheduler))

    def test_regular_cycle_keeps_the_hour_gate(self):
        accounts = [_SchedAccount("g%02d" % i) for i in range(2)]
        scheduler = self.make_scheduler(accounts, hours=_hours_before_now())
        scheduler._run_cycle("验收-整点门禁", catchup_tasks=False)
        for account in accounts:
            self.assertNotIn("checkin", account.calls, "非整点巡检顺手签到了")

    def test_catchup_switch_off_keeps_the_hour_gate(self):
        accounts = [_SchedAccount("off%02d" % i) for i in range(2)]
        wb_settings.set_checkin_startup_catchup(self.dir, False)
        self.addCleanup(wb_settings.set_checkin_startup_catchup, self.dir, True)
        scheduler = self.make_scheduler(accounts, hours=_hours_before_now())
        scheduler._run_cycle("验收-补跑关", catchup_tasks=True)
        for account in accounts:
            self.assertNotIn("checkin", account.calls, "开关关掉仍补跑了签到")


class SchedulerRetryTests(_SchedulerCase):
    """P3-3：签到瞬态失败按 checkin_retries 退避重试，硬失败不重试。"""

    def test_transient_failure_is_retried_with_backoff(self):
        account = _SchedAccount("rt1", checkin_results=[
            {"ok": False, "transient": True, "error": "网络抖动"},
            {"ok": False, "transient": True, "error": "网络抖动"},
            {"ok": True, "msg": "签到成功"},
        ])
        wb_settings.set_checkin_retries(self.dir, 2)
        scheduler = self.make_scheduler([account])
        result = scheduler._checkin_with_retry(account, None)
        self.assertTrue(result.get("ok"), "重试后仍失败：%r" % (result,))
        self.assertEqual(account.calls.count("checkin"), 3,
                         "重试次数不是 1+2：%r" % (account.calls,))
        self.assertEqual(self.sleeps, [5.0, 15.0], "退避不是 5s/15s：%r" % (self.sleeps,))

    def test_hard_failure_is_not_retried(self):
        account = _SchedAccount("rt2", checkin_results=[
            {"ok": False, "transient": False, "error": "账号已被风控"}])
        scheduler = self.make_scheduler([account])
        result = scheduler._checkin_with_retry(account, None)
        self.assertFalse(result.get("ok"))
        self.assertEqual(account.calls.count("checkin"), 1, "硬失败被重试了")
        self.assertEqual(self.sleeps, [], "硬失败不该等待")

    def test_retries_respect_the_batch_deadline(self):
        account = _SchedAccount("rt3", checkin_results=[
            {"ok": False, "transient": True, "error": "网络抖动"}] * 5)
        wb_settings.set_checkin_retries(self.dir, 3)
        scheduler = self.make_scheduler([account])
        result = scheduler._checkin_with_retry(account, time.time() + 2.0)
        self.assertFalse(result.get("ok"))
        self.assertEqual(account.calls.count("checkin"), 1,
                         "截止时间只够一次尝试，却重试了：%r" % (account.calls,))
        self.assertEqual(self.sleeps, [], "5s 退避不该越过 2s 的截止时间")

    def test_retries_setting_zero_means_single_try(self):
        account = _SchedAccount("rt4", checkin_results=[
            {"ok": False, "transient": True, "error": "网络抖动"}] * 3)
        wb_settings.set_checkin_retries(self.dir, 0)
        scheduler = self.make_scheduler([account])
        scheduler._checkin_with_retry(account, None)
        self.assertEqual(account.calls.count("checkin"), 1, "checkin_retries=0 仍重试了")


class SchedulerPlumbingTests(_SchedulerCase):
    """P3-2 巡检前重载账号池、P2-4 每轮一行汇总落盘。"""

    def test_reload_happens_once_per_cycle(self):
        scheduler = self.make_scheduler([_SchedAccount("rl1")])
        scheduler._run_cycle("验收-热重载")
        self.assertEqual(self.pool.reload_calls, 1,
                         "一轮巡检调用了 reload_if_changed %d 次" % self.pool.reload_calls)

    def test_changed_pool_is_logged(self):
        scheduler = self.make_scheduler([_SchedAccount("rl2")])
        self.pool.reload_result = {"changed": True, "checked": 2,
                                  "added": ["u-new"], "removed": ["u-old"]}
        scheduler._run_cycle("验收-热重载变更")
        text = self.log_text(scheduler)
        self.assertIn("重新加载", text)
        # 日志只报计数（账号 uid 清单在 POST /accounts/reload 的 added/removed 里）
        self.assertIn("新增 1 个", text)
        self.assertIn("移除 1 个", text)

    def test_each_cycle_writes_one_summary_line(self):
        scheduler = self.make_scheduler([_SchedAccount("lg1"),
                                         _SchedAccount("lg2", tasks=False)])
        scheduler._run_cycle("验收-日志落盘")
        folder = os.path.join(os.path.dirname(self.dir), "logs")
        names = [n for n in os.listdir(folder) if n.startswith("tasks-")]
        self.assertEqual(len(names), 1, "落盘文件不是恰好一个：%r" % (names,))
        with io.open(os.path.join(folder, names[0]), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("巡检汇总（", text)
        self.assertIn("巡检完成：Token保活", text)
        self.assertIn("候选 ", text)
        summaries = [line for line in text.splitlines() if "巡检汇总（" in line]
        self.assertEqual(len(summaries), 1, "一轮巡检写了 %d 行汇总" % len(summaries))

class SchedulerCheckinSwitchTests(_SchedulerCase):
    """P0-3 + §2.1：checkin_enabled 关闭时调度器不签但照跑其它任务。"""

    def tearDown(self):
        wb_settings.set_checkin_enabled(self.dir, True)
        super().tearDown()

    def test_switch_off_skips_checkin_but_keeps_travel_and_report(self):
        wb_settings.set_checkin_enabled(self.dir, False)
        accounts = [_SchedAccount("sw%d" % index, report_ok=True) for index in range(3)]
        scheduler = self.make_scheduler(accounts)
        scheduler._run_cycle("验收-签到总闸关闭", force=True)
        for account in accounts:
            self.assertNotIn("checkin", account.calls,
                             "总闸关闭却签到了：%r" % (account.calls,))
            self.assertIn("report", account.calls, "上报不该被总闸影响")
        text = self.log_text(scheduler)
        self.assertIn("签到已按设置暂停", text)
        self.assertIn("签到 0", text)

    def test_switch_on_checks_in_as_usual(self):
        wb_settings.set_checkin_enabled(self.dir, True)
        account = _SchedAccount("sw-on")
        scheduler = self.make_scheduler([account])
        scheduler._run_cycle("验收-签到总闸开启", force=True)
        self.assertIn("checkin", account.calls)
        self.assertIn("签到 1", self.log_text(scheduler))

class RunCnTaskCoverageTests(_SchedulerCase):
    """覆盖口径：cn 活跃号必须"被调用"，汇总量只统计真发生动作数（生产口径对齐）。"""

    def test_every_cn_account_is_called_even_when_nothing_moves(self):
        # 187 个 cn 活跃号今晚的真实形态：今日已签到（already_today）、已上报（report_done），
        # 旅行处于 idle+daily_limit_reached / traveling —— 任务必须照样被调用，只是不产生计数。
        accounts = [_SchedAccount("cov%d" % index, checkin_ok=False, report_ok=False)
                    for index in range(5)]
        scheduler = self.make_scheduler(accounts)
        scheduler._run_cycle("验收-覆盖", force=True)
        # travel_calls 里同时记录了 do_cat_travel 与 run_streak_bonus（两者都在旅行段被调用），
        # 所以按账号去重来判"每个 cn 活跃号都被调用过"。
        touched = {account.uid for account in self.travel_calls}
        self.assertEqual(touched, {account.uid for account in accounts},
                         "没被调用旅行任务的账号：%r" % (sorted({a.uid for a in accounts} - touched),))
        for account in accounts:                      # 签到/上报被跳过，但账号确实进了批次
            self.assertEqual(account.calls, [], "%s 不该真的打上游：%r" % (account.uid, account.calls))
        text = self.log_text(scheduler)
        self.assertIn("候选 5", text)
        self.assertIn("旅行 0", text)                  # 没有 claim/depart 就不进汇总量（生产口径）
        self.assertIn("already_today=5", text)

    def test_active_tasks_are_counted_for_every_cn_account(self):
        accounts = [_SchedAccount("act%d" % index, checkin_ok=True, report_ok=True)
                    for index in range(4)]
        scheduler = self.make_scheduler(accounts)
        scheduler._run_cycle("验收-覆盖-全跑", force=True)
        for account in accounts:
            self.assertEqual(account.calls, ["checkin", "report"],
                             "%s 的任务序不对：%r" % (account.uid, account.calls))
        text = self.log_text(scheduler)
        self.assertIn("签到 4", text)
        self.assertIn("上报 4", text)
        self.assertIn("失败 0", text)


class AccountStateTests(unittest.TestCase):
    """账号层（P1-3 / P2-2 / P2-3）：直接对着真实 Account 与临时账号目录。"""

    def setUp(self):
        if not (hasattr(wb_accounts.Account, "credential_usable")
                and hasattr(wb_accounts.Account, "_record_checkin_attempt")):
            print("SKIP: 待实现 -> wb_accounts.Account.credential_usable/_record_checkin_attempt")
            raise unittest.SkipTest("待实现: 账号层字段")
        self._tmp = tempfile.mkdtemp(prefix="wb-verify-acct-")
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def make_account(self, uid="u1", **extra):
        data = {"uid": uid, "accessToken": "token-" + uid, "realm": "cn",
                "nickname": "验收-" + uid}
        data.update(extra)
        account = wb_accounts.Account(data, path=os.path.join(self._tmp, uid + ".json"))
        account.headers = lambda purpose=None: {}
        return account

    def run_checkin(self, account, payload):
        with mock.patch.object(wb_accounts, "http_json", return_value=payload):
            return account.checkin(trigger="manual")

    def test_credential_expiry_blocks_checkin(self):
        account = self.make_account("nomiss")
        self.assertTrue(account.credential_usable())
        account.token_expired = True
        self.assertFalse(account.credential_usable())
        self.assertFalse(account.can_checkin(), "token 过期仍允许签到（P1-3 门槛失效）")
        bare = self.make_account("bare", accessToken="")
        self.assertFalse(bare.credential_usable(), "没有 access_token 也算可用")
        self.assertFalse(bare.can_checkin())

    def test_checked_always_writes_last_checkin(self):
        account = self.make_account("ck")
        account.last_checkin = "2026-10-25 09:00:00"
        self.run_checkin(account, {"code": 0, "msg": "success", "data": {}})
        self.assertTrue(account.last_checkin.startswith(time.strftime("%Y-%m-%d")),
                        "checked 没写 lastCheckin：%r" % (account.last_checkin,))
        self.assertEqual(account.last_checkin_state, "checked")
        self.assertEqual(account.checkin_attempts, 1)
        with io.open(os.path.join(self._tmp, "ck.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved.get("lastCheckinState"), "checked")
        self.assertEqual(int(saved.get("checkinAttempts") or 0), 1)

    def test_already_does_not_overwrite_todays_stamp(self):
        account = self.make_account("al")
        account.last_checkin = "2026-10-25 08:30:00"
        # 上游「今日已签到」的实际文案含「已签到」三字（wb_accounts.checkin_state
        # 的 CHECKIN_ALREADY_HINTS = ("已签到",)），HTTP 400 + 该文案按成功记 already。
        already = {"code": 10001, "msg": "今天已签到"}
        self.run_checkin(account, already)
        first = account.last_checkin
        self.assertTrue(first.startswith(time.strftime("%Y-%m-%d")),
                        "already 首次没写今天：%r" % (first,))
        self.assertEqual(account.last_checkin_state, "already")
        _REAL_SLEEP(1.05)                      # 拉开秒级时间，确保覆写可被发现
        self.run_checkin(account, already)
        self.assertEqual(account.last_checkin, first,
                         "already 覆写了当天的签到时刻（P2-2）")
        self.assertEqual(account.checkin_attempts, 2, "checkinAttempts 没累计")

    def test_auth_failures_disable_the_account(self):
        account = self.make_account("auth")
        wb_settings.set_auto_disable_auth_failures(self._tmp, 3)
        for _ in range(3):
            account.note_error("HTTP 401 Unauthorized")
        self.assertFalse(account.enabled, "连续 3 次认证失败后仍 enabled")
        self.assertFalse(account.tasks_enabled, "连续 3 次认证失败后仍 tasksEnabled")
        # 停用原因写进面板备注 note（last_error 保留上游原文，不能被覆盖）
        self.assertIn("连续 3 次认证失败", account.note or "")
        self.assertIn("HTTP 401 Unauthorized", account.last_error or "")
        with io.open(os.path.join(self._tmp, "auth.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertFalse(saved.get("enabled"), "自动停用没有落盘")
        self.assertIn("连续 3 次认证失败", saved.get("note") or "")

    def test_threshold_zero_keeps_the_account(self):
        account = self.make_account("keep")
        wb_settings.set_auto_disable_auth_failures(self._tmp, 0)
        for _ in range(6):
            account.note_error("HTTP 401 Unauthorized")
        self.assertTrue(account.enabled, "阈值 0（关闭）仍把账号停用了")
        self.assertTrue(account.tasks_enabled)

    def test_success_clears_the_auth_streak(self):
        account = self.make_account("clr")
        wb_settings.set_auto_disable_auth_failures(self._tmp, 3)
        account.note_error("HTTP 401 Unauthorized")
        account.note_error("HTTP 401 Unauthorized")
        account.note_success()
        self.assertEqual(account.auth_fail_streak, 0, "成功后计数没清零")
        account.note_error("HTTP 401 Unauthorized")
        self.assertTrue(account.enabled, "清零后又错一次就被停用了")


class PoolReloadTests(unittest.TestCase):
    """P3-2（账号池侧）：reload_if_changed 的签名判定与新增/移除清单。"""

    def setUp(self):
        if not hasattr(wb_accounts.AccountPool, "reload_if_changed"):
            print("SKIP: 待实现 -> wb_accounts.AccountPool.reload_if_changed")
            raise unittest.SkipTest("待实现: reload_if_changed")
        self._tmp = tempfile.mkdtemp(prefix="wb-verify-pool-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.logs = []

    def write_account(self, uid):
        payload = {"uid": uid, "accessToken": "token-" + uid, "realm": "cn"}
        with io.open(os.path.join(self._tmp, uid + ".json"), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_reload_reports_added_and_removed(self):
        self.write_account("a1")
        self.write_account("a2")
        pool = wb_accounts.AccountPool(self._tmp, log=self.logs.append)
        pool.load()
        first = pool.reload_if_changed()
        self.assertIs(first.get("changed"), False,
                      "首次调用只该记签名，不改池子：%r" % (first,))
        same = pool.reload_if_changed()
        self.assertIs(same.get("changed"), False, "目录没变也说变了：%r" % (same,))
        self.write_account("a3")
        changed = pool.reload_if_changed()
        self.assertIs(changed.get("changed"), True, "新增账号文件没被察觉：%r" % (changed,))
        self.assertEqual(sorted(changed.get("added") or []), ["a3"])
        self.assertEqual(list(changed.get("removed") or []), [])
        self.assertEqual(len(pool.accounts), 3)
        os.remove(os.path.join(self._tmp, "a1.json"))
        _REAL_SLEEP(0.05)
        removed = pool.reload_if_changed()
        self.assertIs(removed.get("changed"), True, "删除账号文件没被察觉：%r" % (removed,))
        self.assertEqual(sorted(removed.get("removed") or []), ["a1"])
        self.assertEqual(len(pool.accounts), 2)
        self.assertTrue(any("重新加载" in str(line) or "已变化" in str(line)
                            for line in self.logs),
                        "重载没有留下日志：%r" % (self.logs,))


class CheckinUpstreamFlagsTests(unittest.TestCase):
    """P2-2 / P3-3 的上游回执分类：HTTP 400+「已签到」算成功，5xx/429 算瞬态，401 算认证失败。

    这三条是 /accounts/checkin 的对外语义变化（400+已签到由 ok=False 变 ok=True）
    与调度器重试（P3-3 只重试 transient）的共同地基，因此单独钉住。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="verify-upstream-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make_account(self, uid="uacct001", **extra):
        payload = {"uid": uid, "accessToken": "tok-verify", "realm": "cn", "nickname": uid}
        payload.update(extra)
        path = os.path.join(self.dir, uid + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        with open(path, encoding="utf-8") as handle:
            account = wb_accounts.Account(json.load(handle), path)
        account.headers = lambda purpose=None: {}
        return account

    def raise_http_error(self, code, payload=None, reason="err"):
        """构造一个带 JSON 正文的 HTTPError（上游 400/401/429/500 的真实形状）。"""
        body = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")
        error = urllib.error.HTTPError("https://upstream.invalid/billing/checkin",
                                       code, reason, {}, io.BytesIO(body))
        return mock.patch.object(wb_accounts, "http_json", side_effect=error)

    def persisted(self, account):
        with open(account.path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_http_400_already_counts_as_success(self):
        account = self.make_account()
        today = time.strftime("%Y-%m-%d")
        account.last_checkin = today + " 08:30:00"
        account.last_checkin_state = "checked"
        with self.raise_http_error(400, {"code": 10001, "msg": "今天已签到"}):
            result = account.checkin(trigger="manual")
        self.assertTrue(result.get("ok"), "HTTP 400 + 已签到 必须按成功处理：%r" % (result,))
        self.assertEqual(result.get("state"), "already")
        self.assertFalse(result.get("transient"))
        self.assertFalse(result.get("auth_failed"))
        self.assertEqual(account.last_checkin, today + " 08:30:00",
                         "already 分支不许覆写今天的 lastCheckin")
        self.assertEqual(account.checkin_attempts, 1)
        stored = self.persisted(account)
        self.assertEqual(stored.get("lastCheckinState"), "already")
        self.assertEqual(stored.get("lastCheckin"), today + " 08:30:00")

    def test_http_400_already_writes_stamp_when_missing(self):
        account = self.make_account()
        account.last_checkin = "2026-10-25 08:30:00"
        with self.raise_http_error(400, {"code": 10001, "msg": "今天已签到"}):
            result = account.checkin(trigger="manual")
        self.assertTrue(result.get("ok"))
        self.assertTrue(str(account.last_checkin or "").startswith(time.strftime("%Y-%m-%d")),
                        "昨天没签过时 already 必须补写今天的时刻")

    def test_http_500_is_transient(self):
        account = self.make_account()
        with self.raise_http_error(500, {"code": -1, "msg": "internal error"}):
            result = account.checkin(trigger="manual")
        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("transient"), "5xx 必须标 transient，调度器才会重试")
        self.assertFalse(result.get("auth_failed"))
        self.assertIsNotNone(account.last_checkin_at, "失败也要落 lastCheckinAt")

    def test_http_429_is_transient(self):
        account = self.make_account()
        with self.raise_http_error(429, {"msg": "too many requests"}):
            result = account.checkin(trigger="manual")
        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("transient"))

    def test_http_401_is_auth_failed_not_transient(self):
        account = self.make_account()
        with self.raise_http_error(401, {"msg": "unauthorized"}):
            result = account.checkin(trigger="manual")
        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("auth_failed"), "401 必须标 auth_failed（重试只会再吃 401）")
        self.assertFalse(result.get("transient"))

    def test_network_error_is_transient(self):
        account = self.make_account()
        with mock.patch.object(wb_accounts, "http_json", side_effect=OSError("connection reset")):
            result = account.checkin(trigger="manual")
        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("transient"))
        self.assertFalse(result.get("auth_failed"))
        self.assertEqual(account.last_checkin_state, "failed")
        self.assertEqual(self.persisted(account).get("lastCheckinState"), "failed")


class ReloadSelfWriteRaceTests(unittest.TestCase):
    """P3-2 回归：生产 20:11:04 那次 reload 误判的独立复现。

    事故：Account.save() 里 os.replace 已经生效、_SELF_WRITTEN_FILES 还没登记的
    微秒级窗口，被 reload_if_changed() 扫到 → 判成外部改动 → 整池重建，于是
    token_expired / 冷却 / auth_fail_streak 全被清空（那一轮 187 个号在持续写盘）。
    这里不 mock 被测函数：真的跑 Account.save()，只把「reload 恰好落在 os.replace
    之后」这一刻同步出来，验证 _confirm_external() 的复核兜底。
    """

    def setUp(self):
        missing = _skip_missing_reload()
        if missing:
            print("SKIP: 待实现 -> %s" % ", ".join(missing))
            raise unittest.SkipTest("reload 待实现")
        self.dir = tempfile.mkdtemp(prefix="verify-reload-race-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.logs = []
        self.write("a1", {"realm": "cn"})
        self.pool = wb_accounts.AccountPool(self.dir, log=self.logs.append)
        self.pool.load()
        self.pool.reload_if_changed()          # 首次调用只记签名

    def write(self, uid, extra=None):
        data = {"uid": uid, "accessToken": "token-" + uid, "nickname": uid}
        data.update(extra or {})
        path = os.path.join(self.dir, uid + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        return path

    def test_self_write_only_reports_unchanged_and_keeps_runtime_state(self):
        account = self.pool.get("a1")
        self.assertIsNotNone(account)
        account.token_expired = True
        account.auth_fail_streak = 2
        account.cooldown_until = time.time() + 600
        account.save(self.dir)                 # 自己写盘：目录签名必然变了
        result = self.pool.reload_if_changed()
        self.assertFalse(result.get("changed"), "自己 save() 写下去的文件不算目录变化")
        self.assertIs(self.pool.get("a1"), account, "不许整池重建（对象身份要保留）")
        self.assertTrue(account.token_expired, "运行态 token_expired 不许被 reload 清掉")
        self.assertEqual(account.auth_fail_streak, 2)
        self.assertGreater(account.cooldown_until, time.time())

    def test_reload_right_after_replace_still_reports_unchanged(self):
        """把 reload 精确塞进 os.replace 之后那一刻：必须靠复核洗白，不许 changed。"""
        account = self.pool.get("a1")
        account.token_expired = True
        account.auth_fail_streak = 3
        replaced = threading.Event()
        outcomes = []
        errors = []
        real_replace = wb_accounts.os.replace

        def replace_then_signal(src, dst):
            outcome = real_replace(src, dst)
            replaced.set()
            return outcome

        def worker():
            try:
                account.save(self.dir)
            except Exception as exc:           # pragma: no cover - 只在实现炸时触发
                errors.append(repr(exc))

        with mock.patch.object(wb_accounts.os, "replace", replace_then_signal):
            thread = threading.Thread(target=worker, name="verify-save")
            thread.start()
            self.assertTrue(replaced.wait(5.0), "save() 没走到 os.replace")
            outcomes.append(self.pool.reload_if_changed())
            thread.join(5.0)
        self.assertFalse(thread.is_alive(), "save() 线程没结束")
        self.assertEqual(errors, [])
        self.assertFalse(outcomes[0].get("changed"),
                         "replace 已生效但登记表可能还没跟上：复核必须洗白这次误判")
        self.assertIs(self.pool.get("a1"), account)
        self.assertTrue(account.token_expired)
        self.assertEqual(account.auth_fail_streak, 3)

    def test_external_change_is_still_detected(self):
        """反向判据：真的外部改动不许被复核逻辑吞掉。"""
        self.write("a2", {"realm": "cn"})
        result = self.pool.reload_if_changed()
        self.assertTrue(result.get("changed"), "外部新增必须判出来")
        self.assertIn("a2", result.get("added") or [])
        self.assertIsNotNone(self.pool.get("a2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
