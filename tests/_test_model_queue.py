"""Shared model-queue Gate: 6020/6021 are endpoint limits, not credentials.

Unit tests use a controllable monotonic clock. Route tests run the real
AccountPool and real pinned-product/realm fallback functions with a stubbed
HTTP transport. No production accounts, credentials or upstream calls.
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs
_TMP = isolated_data_dirs("wb-model-queue-")
import wb_accounts
import wb_model_queue as Q
import wb_proxy as P
import wb_settings

MODEL = "kimi-k3.1-pro"


def account(uid, product="workbuddy", realm="cn"):
    return wb_accounts.Account({
        "uid": uid, "realm": realm, "product": product,
        "accessToken": "test-" + uid, "enabled": True,
    })


def http_error(request, detail, status=429):
    return urllib.error.HTTPError(
        request.full_url, status, "synthetic upstream", {},
        io.BytesIO(detail.encode("utf-8")))


class Response(object):
    status = 200
    def close(self):
        return None
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def response():
    return Response()


class Pool(object):
    def __init__(self, accounts):
        self.accounts = accounts
        self.affinity = types.SimpleNamespace(demote=lambda *a: None)
        self.picks = []

    def count_ready(self, realm, model=None, allowed=None):
        return sum(a.realm == realm and a.ready(model=model)
                   and (allowed is None or a.uid in allowed)
                   for a in self.accounts)

    def pick_for_session(self, realm, session_key=None, exclude=(), model=None,
                         page=None, allowed=None):
        picked = next((a for a in self.accounts if a.realm == realm
                       and a.uid not in exclude and a.ready(model=model)
                       and (allowed is None or a.uid in allowed)), None)
        self.picks.append(picked.uid if picked else None)
        return picked

    def apply_remaining_weights(self, weights=None):
        return None


class Clock(object):
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


class GateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.gate = Q.ModelQueueGate(self.clock)
        self.scope = ("cn", MODEL, "workbuddy", "https://edge.invalid")

    def test_structured_shapes_and_units(self):
        samples = [
            (json.dumps({"code": 6020, "data": {"retry_after": 40,
                         "estimated_wait": 16195, "queue_position": 3239}}), 6020, 40),
            (json.dumps({"error": {"data": {"code": "6020",
                         "data": {"retry_after": "7"}}}}), 6020, 7),
            (json.dumps({"data": {"error": {"code": 6021, "msg": "full"}}}), 6021, None),
        ]
        for detail, code, hint in samples:
            actual, data = Q.classify(detail)
            self.assertEqual(actual, code)
            self.assertEqual(data.get("retry_after"), str(hint) if hint == 7 else hint)
        for bad in ('queue.full.title code 6021', '{"msg":"6021"}',
                    '{"code":6004,"data":{"retry_after":40}}', None, 'not json'):
            self.assertEqual(Q.classify(bad), (None, {}))
        self.assertEqual(Q._integer(True), None)
        self.assertEqual(Q._integer("40.0"), None)

    def test_open_half_open_success_and_backoff(self):
        proceed, wait, ticket = self.gate.begin(self.scope)
        self.assertEqual((proceed, wait), (True, 0))
        self.assertEqual(Q.ModelQueueGate._probe_token(ticket), False)
        self.assertEqual(self.gate.note(self.scope), 5)
        self.assertEqual(self.gate.begin(self.scope)[:2], (False, 5))
        self.clock.now = 5.0
        proceed, wait, token = self.gate.begin(self.scope)
        self.assertTrue(proceed)
        self.assertNotEqual(token, False)
        self.assertEqual(self.gate.begin(self.scope)[:2], (False, 1))
        self.assertEqual(self.gate.begin(self.scope)[2], False)
        self.assertTrue(self.gate.success(self.scope, token))
        proceed, wait, ticket = self.gate.begin(self.scope)
        self.assertEqual((proceed, wait), (True, 0))
        self.assertEqual(Q.ModelQueueGate._probe_token(ticket), False)
        self.assertEqual(self.gate.snapshot(), [])
        waits = [self.gate.note(self.scope) for _ in range(7)]
        self.assertEqual(waits, [5, 10, 20, 40, 60, 60, 60])

    def test_foreign_results_never_steal_probe_or_fresh_evidence(self):
        self.gate.note(self.scope, 5)
        self.clock.now = 5.0
        _, _, token = self.gate.begin(self.scope)
        # A request admitted before the Gate opened cannot steal the live probe.
        stale = (0, False)
        self.assertEqual(self.gate.fail(self.scope, stale, 5), 5)
        # The newer real probe remains authoritative over every older response.
        self.assertTrue(self.gate.success(self.scope, token))
        # A closed tombstone means there is no congestion to wait out.
        self.assertEqual(self.gate.fail(self.scope, stale, 5), 1)
        self.assertEqual(self.gate.check(self.scope), (True, 0))
        self.assertFalse(self.gate.success(self.scope, token))
        self.gate.note(self.scope, 5)
        self.clock.now = 10.0
        _, _, token = self.gate.begin(self.scope)
        self.assertNotEqual(token, False)
        self.clock.now = 11.0
        self.assertEqual(self.gate.begin(self.scope)[2], False)
        self.assertFalse(self.gate.success(self.scope, False))
        self.gate.abandon(self.scope, token)
        _, _, token = self.gate.begin(self.scope)
        self.assertNotEqual(token, False)
        self.assertTrue(self.gate.success(self.scope, token))

        # A response from one product never clears another product scope,
        # even when both products currently use the same endpoint.
        other = ("cn", MODEL, "vscode", "https://edge.invalid")
        self.gate.note(other, 40)
        self.assertEqual(len(self.gate.snapshot()), 1)
        self.clock.now = 51.0
        _, _, other_token = self.gate.begin(other)
        self.gate.success(self.scope)
        self.assertEqual(self.gate.check(other)[0], False)
        self.assertTrue(self.gate.success(other, other_token))

    def test_retry_hint_abandon_lease_expiry_and_snapshot(self):
        self.assertEqual(self.gate.note(self.scope, 40), 40)
        self.assertEqual(self.gate.note(self.scope, -1), 40)
        self.assertEqual(self.gate.note(self.scope, 1000000), 3600)
        self.clock.now = 3600.0
        proceed, wait, token = self.gate.begin(self.scope)
        self.assertTrue(proceed)
        self.assertEqual(self.gate.check(self.scope)[:2], (False, 1))
        self.assertEqual(self.gate.snapshot()[0]["phase"], "half_open")
        self.gate.abandon(self.scope, token)
        _, _, token = self.gate.begin(self.scope)
        self.assertNotEqual(token, False)
        self.gate.abandon(self.scope, token)
        self.clock.now += 180.0
        _, _, token = self.gate.begin(self.scope)
        self.clock.now += 180.0
        _, _, new_token = self.gate.begin(self.scope)
        self.assertNotEqual(token, new_token)
        self.assertFalse(self.gate.success(self.scope, token))
        # A response admitted before the newer attempt is stale too, even
        # after that newer attempt's crash lease has expired.
        self.assertFalse(self.gate.success(self.scope))
        self.assertTrue(self.gate.success(self.scope, new_token))

        # An old ordinary success must not erase expired queue evidence either:
        # recovery belongs to a real half-open request, not wall-clock expiry.
        self.gate.note(self.scope, 1)
        self.clock.now += 2.0
        self.assertFalse(self.gate.success(self.scope))
        self.assertFalse(self.gate.begin(self.scope)[2] is False)

    def test_scopes_stale_tokens_and_shared_endpoint_recovery(self):
        other_product = ("cn", MODEL, "cli", self.scope[3])
        other_endpoint = ("cn", MODEL, "vscode", "https://other.invalid")
        other_model = ("cn", "another", "workbuddy", self.scope[3])
        self.gate.note(other_product)
        self.gate.note(other_endpoint)
        self.gate.note(other_model)
        self.assertEqual(self.gate.check(("cn", "unseen-model", "p", "e")), (True, 0))
        self.clock.now = 2.0
        self.assertEqual(self.gate.check(other_product)[0], False)
        self.assertEqual(self.gate.check(("intl", "unrelated", "p", "e")), (True, 0))
        self.gate.success(self.scope)
        self.assertEqual(self.gate.check(other_product)[0], False)
        self.assertEqual(self.gate.begin(other_endpoint)[0], False)
        self.assertEqual(self.gate.begin(other_model)[0], False)
        self.clock.now = 5.0
        _, _, token = self.gate.begin(other_model)
        self.assertTrue(self.gate.success(other_model, token))
        self.assertFalse(self.gate.success(other_model, token))
        self.assertEqual(self.gate.check(other_model), (True, 0))
        _, _, token = self.gate.begin(other_model)
        self.gate.note(other_model, 60)
        self.assertFalse(self.gate.success(other_model, token))
        self.assertEqual(self.gate.check(other_model)[0], False)

    def test_concurrent_singleflight(self):
        self.gate.note(self.scope, 0)
        self.clock.now = 1.0
        barrier = threading.Barrier(33)
        results = []
        lock = threading.Lock()
        def worker():
            barrier.wait()
            result = self.gate.begin(self.scope)
            with lock: results.append(result)
        threads = [threading.Thread(target=worker) for _ in range(32)]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join(5)
        self.assertEqual(sum(r[0] and r[2] is not False for r in results), 1)
        self.assertEqual(sum(not r[0] for r in results), 31)
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_bounded_retention(self):
        old_max = Q.MAX_SCOPES
        Q.MAX_SCOPES = 8
        try:
            for n in range(30): self.gate.note((str(n), "m", "p", "e"), 60)
        finally: Q.MAX_SCOPES = old_max
        self.assertLessEqual(len(self.gate.states), 8)
        self.assertLessEqual(len(self.gate.probes), 8)

    def test_retention_never_evicts_fresh_queue_evidence(self):
        old_max = Q.MAX_SCOPES
        Q.MAX_SCOPES = 2
        try:
            for name in ("A", "B"):
                scope = (name, "m", "p", "e")
                self.gate.note(scope, 1)
                self.clock.now += 1.0
                self.assertTrue(self.gate.begin(scope)[0])
            fresh = ("new", "m", "p", "e")
            self.gate.note(fresh, 1)
            self.assertIn(fresh, self.gate.states)
            self.assertEqual(self.gate.begin(fresh)[:2], (False, 1))
        finally:
            Q.MAX_SCOPES = old_max

    def test_retention_prefers_expired_unprobed_history(self):
        old_max = Q.MAX_SCOPES
        Q.MAX_SCOPES = 2
        try:
            expired = ("expired", "m", "p", "e")
            active = ("active", "m", "p", "e")
            self.gate.note(expired, 1)
            self.gate.note(active, 60)
            self.clock.now = 2.0
            self.gate.note(("fresh", "m", "p", "e"), 60)
            self.assertNotIn(expired, self.gate.states)
            self.assertIn(active, self.gate.states)
            self.assertLessEqual(len(self.gate.states), 2)
        finally:
            Q.MAX_SCOPES = old_max


    def test_wait_budget_cancellation_and_clear_wakeup(self):
        # A closed Gate never sleeps, and a finite Gate never becomes an
        # infinite retry. These are real wall-clock waits, not mock sleeps.
        self.assertTrue(self.gate.wait(self.scope, 0.01))
        self.gate.note(self.scope, 30)
        started = time.monotonic()
        self.assertFalse(self.gate.wait(self.scope, 0.03))
        self.assertLess(time.monotonic() - started, 0.5)
        with self.assertRaises(Q.QueueWaitCancelled):
            self.gate.wait(self.scope, 30, cancelled=lambda: True)
        self.assertEqual(self.gate.waiters, {})
        result = []
        waiter = threading.Thread(target=lambda: result.append(
            self.gate.wait(self.scope, 10)))
        waiter.start()
        deadline = time.monotonic() + 2
        while not self.gate.waiters and time.monotonic() < deadline:
            time.sleep(0.005)
        self.gate.clear()
        waiter.join(2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(self.gate.waiters, {})


class RouteTests(unittest.TestCase):
    def setUp(self):
        P.MODEL_QUEUE_GATE.clear()
        self.addCleanup(P.MODEL_QUEUE_GATE.clear)
        self.clock = Clock()
        self.gate_patch = mock.patch.object(P, "MODEL_QUEUE_GATE",
                                             Q.ModelQueueGate(self.clock))
        self.gate_patch.start()
        self.addCleanup(self.gate_patch.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="wb-queue-routes-")
        self.addCleanup(self.tmp.cleanup)
        self.accounts = [account("a" * 32), account("b" * 32)]
        self.pool = Pool(self.accounts)
        self.patch = mock.patch.multiple(
            P, POOL=self.pool, CURRENT_REALM="cn", ACCOUNTS_DIR=self.tmp.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.calls = []
        self.plan = []
        def transport(req, timeout=None, proxy=None):
            self.calls.append(req)
            action = self.plan.pop(0) if self.plan else response()
            if isinstance(action, Exception): raise action
            return action
        self.transport = mock.patch.object(wb_accounts, "urlopen", transport)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def payload(self, model=MODEL):
        return {"model": model, "messages": [{"role": "user", "content": "hi"}]}

    def call(self, **kwargs):
        return P.open_upstream(self.payload(), target_realm="cn", **kwargs)

    def queue_error(self, code=6021, hint=None):
        body = {"code": code, "msg": "queue.full.title"}
        if hint is not None: body["data"] = {"retry_after": hint, "estimated_wait": 999999}
        return json.dumps(body)

    def test_one_queue_error_one_upstream_call_and_no_account_penalty(self):
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"),
                                self.queue_error(6021))]
        with self.assertRaises(P.ModelQueueLimited) as raised:
            self.call(session_key="keep-this-affinity")
        exc = raised.exception
        self.assertEqual(exc.upstream_code, 6021)
        self.assertIsNone(exc.account_uid)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.pool.picks), 1)
        for a in self.accounts:
            self.assertEqual(a.model_cooldowns, {})
            self.assertEqual(a.model_cooldowns_at, {})
            self.assertEqual(a.soft_streak, 0)
            self.assertEqual(a.fails, 0)
            self.assertTrue(a.ready(model=MODEL))
        self.assertEqual(P.MODEL_QUEUE_GATE.snapshot()[0]["backoff_streak"], 1)

    def test_large_queue_json_still_avoids_rotation_and_cooldown(self):
        body = json.dumps({"padding": "x" * (1024 * 1024), "code": 6021,
                           "msg": "queue.full.title"})
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"), body)]
        with self.assertRaises(P.ModelQueueLimited) as raised: self.call()
        self.assertEqual(raised.exception.upstream_code, 6021)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(not a.model_cooldowns for a in self.accounts))

    def test_open_fast_fail_before_pick_and_separate_model(self):
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"),
                                self.queue_error(6021))]
        with self.assertRaises(P.ModelQueueLimited): self.call()
        self.pool.picks = []
        with self.assertRaises(P.ModelQueueLimited) as raised:
            self.call()
        self.assertEqual(self.pool.picks, [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(raised.exception.detail, "")
        resp, picked, _ = P.open_upstream(self.payload("deepseek-v4.1-flash"), target_realm="cn")
        self.assertIsNotNone(picked)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.calls), 2)

    def test_half_open_then_recovery_without_rescanning_pool(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 1)
        P.MODEL_QUEUE_GATE.states[scope]["until"] = self.clock.now - 1
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"),
                                self.queue_error(6020, 40))]
        with self.assertRaises(P.ModelQueueLimited) as raised: self.call()
        self.assertEqual(raised.exception.wait, 40)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(P.MODEL_QUEUE_GATE.snapshot()[0]["retry_after"], 40)
        # A stale product entry must not falsely claim recovery for the real
        # account scope; the actual successful request clears its own scope.
        with self.assertRaises(P.ModelQueueLimited): self.call()
        self.assertEqual(len(self.pool.picks), 1)
        P.MODEL_QUEUE_GATE.states[scope]["until"] = self.clock.now - 1
        self.plan = [response()]
        resp, _, _ = self.call()
        self.assertEqual(resp.status, 200)
        self.assertEqual(P.MODEL_QUEUE_GATE.snapshot(), [])
        self.assertEqual(len(self.pool.picks), 2)
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"),
                                self.queue_error(6021))]
        with self.assertRaises(P.ModelQueueLimited):
            self.call()
        self.assertEqual(self.pool.picks[-1], self.accounts[0].uid)

    def test_pinned_scope_and_account_slot_released(self):
        pinned = self.accounts[0]
        with mock.patch.object(P.wb_settings, "account_max_concurrent", return_value=1):
            self.assertTrue(P.account_slot_acquire(pinned, 1))
            self.assertFalse(P.account_slot_acquire(pinned, 1))
            P.account_slot_release(pinned)
            self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"),
                                    self.queue_error())]
            with self.assertRaises(P.ModelQueueLimited): self.call(pin_account=pinned)
            self.assertEqual(P.account_inflight_snapshot(), {})
            self.assertTrue(P.account_slot_acquire(pinned, 1))
            P.account_slot_release(pinned)
        with self.assertRaises(P.ModelQueueLimited): self.call(pin_account=pinned)
        self.assertEqual(len(self.calls), 1)

    def test_pinned_401_refresh_retries_same_account(self):
        pinned = self.accounts[0]
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"), "{}", status=401), response()]
        with mock.patch.object(pinned, "refresh", return_value=True):
            resp, picked, _ = self.call(pin_account=pinned)
        self.assertEqual(resp.status, 200)
        self.assertEqual(picked.uid, pinned.uid)
        self.assertEqual(len(self.calls), 2)

    def test_local_fast_fail_releases_account_slot(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 40)
        with mock.patch.object(P.wb_settings, "account_max_concurrent", return_value=1):
            with self.assertRaises(P.ModelQueueLimited): self.call()
            self.assertEqual(P.account_inflight_snapshot(), {})
            self.assertTrue(P.account_slot_acquire(self.accounts[0], 1))
            P.account_slot_release(self.accounts[0])
        self.assertEqual(self.calls, [])

    def test_allowed_accounts_does_not_leak_other_scope(self):
        other = account("c" * 32, product="vscode")
        self.pool.accounts.append(other)
        scope = Q.queue_scope(other, MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 60)
        # An out-of-key scope is not probed by a restricted caller.
        self.assertFalse(P.MODEL_QUEUE_GATE.check(scope)[0])
        self.plan = [response()]
        resp, picked, _ = self.call(allowed_accounts={a.uid for a in self.accounts})
        self.assertIn(picked.uid, {a.uid for a in self.accounts})

    def test_product_switch_has_independent_scope(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 60)
        with mock.patch.object(P.wb_settings, "auto_switch_product", return_value=True), \
             mock.patch.object(P.wb_settings, "auto_switch_products", return_value=["vscode"]):
            self.plan = [response()]
            resp, picked, _ = self.call()
            self.assertEqual(picked.product, "vscode")
            self.assertEqual(resp.status, 200)
            self.assertTrue(self.calls[0].full_url.startswith(picked.chat_base_url()),
                            self.calls[0].full_url)

    def test_only_nondefault_product_ignores_default_scope(self):
        other = account("c" * 32, product="vscode")
        self.pool.accounts = [other]
        default_scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(default_scope, 60)
        resp, picked, _ = self.call()
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(picked.uid, other.uid)
        self.assertTrue(self.calls[0].full_url.startswith(other.chat_base_url()))

    def test_non_429_http_error_releases_half_open_for_retry(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 0)
        P.MODEL_QUEUE_GATE.states[scope]["until"] = self.clock.now - 1
        self.plan = [
            http_error(types.SimpleNamespace(full_url="https://edge.invalid"),
                       '{"code":50001,"msg":"server error"}', status=500),
            response(),
        ]
        resp, picked, _ = self.call()
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(picked.uid, self.accounts[1].uid)

    def test_transport_error_does_not_strand_lease(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 0)
        P.MODEL_QUEUE_GATE.states[scope]["until"] = self.clock.now - 1
        self.plan = [TimeoutError("synthetic timeout"), response()]
        with mock.patch.object(P, "is_transient", return_value=False), \
             mock.patch.object(P, "looks_like_local_network", return_value=False):
            resp, _, _ = self.call()
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.calls), 2)

    def test_probe_inconclusive_retry_can_start_new_singleflight(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 0)
        P.MODEL_QUEUE_GATE.states[scope]["until"] = self.clock.now - 1
        self.plan = [TimeoutError("first probe timeout"), response()]
        resp, _, _ = self.call()
        self.assertEqual(resp.status, 200)
        self.assertEqual(P.MODEL_QUEUE_GATE.snapshot(), [])

    def test_normal_success_keeps_real_pool_rotation_and_affinity(self):
        pool = wb_accounts.AccountPool(self.tmp.name)
        pool.accounts = list(self.accounts)
        with mock.patch.object(P, "POOL", pool):
            selected = []
            for name in ("session-A", "session-B", "session-C", "session-A"):
                self.plan = [response()]
                resp, picked, _ = self.call(session_key=name)
                self.assertEqual(resp.status, 200)
                selected.append(picked.uid)
            self.assertEqual(selected, [self.accounts[0].uid, self.accounts[1].uid,
                                       self.accounts[0].uid, self.accounts[0].uid])
            self.assertEqual(len(self.calls), 4)
            self.assertEqual(P.MODEL_QUEUE_GATE.snapshot(), [])

    def test_realm_fallback_keeps_open_queue_clock(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 40)
        P.MODEL_QUEUE_GATE.states[scope]["until"] = self.clock.now + 40
        calls = []
        def open_route(*args, **kwargs):
            calls.append(kwargs.get("target_realm"))
            raise P.ModelQueueLimited(wait=40, scope=scope)
        with mock.patch.object(P, "open_upstream", open_route), \
             mock.patch.object(P, "realm_fallback_enabled", return_value=True), \
             mock.patch.object(P, "realm_fallback_target", return_value="cn"):
            with self.assertRaises(P.ModelQueueLimited): P.open_upstream_with_fallback(self.payload(), target_realm="cn")
        self.assertEqual(calls, ["cn"])

    def test_pretransport_fault_releases_lease_and_slot(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 1)
        P.MODEL_QUEUE_GATE.states[scope]["until"] = self.clock.now - 1
        with mock.patch.object(P.wb_settings, "account_max_concurrent", return_value=1), \
             mock.patch.object(P, "encode_upstream_body", side_effect=RuntimeError("encode failed")):
            with self.assertRaisesRegex(RuntimeError, "encode failed"): self.call()
        self.assertEqual(P.MODEL_QUEUE_GATE.probes, {})
        self.assertEqual(P.account_inflight_snapshot(), {})
        self.assertEqual(self.calls, [])
        resp, _, _ = self.call()
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.calls), 1)

    def test_pool_busy_releases_preflight_probe(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 1)
        P.MODEL_QUEUE_GATE.states[scope]["until"] = self.clock.now - 1
        self.pool.accounts = self.accounts[:1]
        with mock.patch.object(P.wb_settings, "account_max_concurrent", return_value=1), \
             mock.patch.object(P, "ACCOUNT_SLOT_WAIT_MAX", 0):
            self.assertTrue(P.account_slot_acquire(self.accounts[0], 1))
            try:
                with self.assertRaises(P.RateLimited): self.call()
            finally:
                P.account_slot_release(self.accounts[0])
        self.assertEqual(P.MODEL_QUEUE_GATE.probes, {})
        self.assertEqual(P.account_inflight_snapshot(), {})
        resp, _, _ = self.call()
        self.assertEqual(resp.status, 200)

    def test_mixed_product_pool_tries_healthy_endpoint(self):
        other = account("c" * 32, product="vscode")
        self.pool.accounts = [self.accounts[0], other]
        P.MODEL_QUEUE_GATE.note(Q.queue_scope(self.accounts[0], MODEL), 40)
        resp, picked, _ = self.call()
        self.assertEqual(resp.status, 200)
        self.assertEqual(picked.uid, other.uid)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0].full_url.startswith(other.chat_base_url()))
        self.assertTrue(all(not a.model_cooldowns for a in self.pool.accounts))

    def test_product_scope_snapshot_survives_concurrent_switch(self):
        pinned = self.accounts[0]
        old_scope = Q.queue_scope(pinned, MODEL)
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"),
                                self.queue_error(6021, 19))]
        def switch_after_queue_retarget(*args, **kwargs):
            # Headers are after the queue scope snapshot but before urlopen.
            # A shared Account can switch in another request at this point.
            pinned.set_product("vscode")
            return {"Authorization": "Bearer test"}
        with mock.patch.object(pinned, "headers", side_effect=switch_after_queue_retarget):
            with self.assertRaises(P.ModelQueueLimited) as raised:
                self.call(pin_account=pinned)
        self.assertEqual(raised.exception.queue_scope, old_scope)
        self.assertTrue(self.calls[0].full_url.startswith(old_scope[3]))
        self.assertFalse(P.MODEL_QUEUE_GATE.check(old_scope)[0])
        self.assertTrue(P.MODEL_QUEUE_GATE.check(Q.queue_scope(pinned, MODEL))[0])

    def test_all_mixed_product_scopes_open_uses_shortest_wait(self):
        other = account("c" * 32, product="vscode")
        self.pool.accounts = [self.accounts[0], other]
        P.MODEL_QUEUE_GATE.note(Q.queue_scope(self.accounts[0], MODEL), 40)
        P.MODEL_QUEUE_GATE.note(Q.queue_scope(other, MODEL), 12)
        with self.assertRaises(P.ModelQueueLimited) as raised:
            self.call()
        self.assertEqual(raised.exception.wait, 12)
        self.assertEqual(raised.exception.queue_scope, Q.queue_scope(other, MODEL))
        self.assertEqual(self.calls, [])
        self.assertEqual(P.account_inflight_snapshot(), {})

    def test_mixed_allowed_subset_open_preserves_queue_error(self):
        allowed = account("a" * 32)
        other = account("c" * 32, product="vscode")
        self.pool.accounts = [allowed, other]
        scope = Q.queue_scope(allowed, MODEL)
        P.MODEL_QUEUE_GATE.note(scope, 27)
        with self.assertRaises(P.ModelQueueLimited) as raised:
            self.call(allowed_accounts={allowed.uid})
        self.assertEqual((raised.exception.wait, raised.exception.queue_scope), (27, scope))
        self.assertEqual(self.calls, [])

    def test_all_mixed_product_scopes_open_preserves_queue_error(self):
        other = account("c" * 32, product="vscode")
        self.pool.accounts = [self.accounts[0], other]
        for candidate in self.pool.accounts:
            P.MODEL_QUEUE_GATE.note(Q.queue_scope(candidate, MODEL), 40)
        with self.assertRaises(P.ModelQueueLimited) as raised:
            self.call()
        self.assertEqual(raised.exception.wait, 40)
        self.assertIsNotNone(raised.exception.queue_scope)
        self.assertEqual(self.calls, [])
        self.assertEqual(P.account_inflight_snapshot(), {})
        self.assertTrue(all(a.fails == 0 and not a.model_cooldowns
                            for a in self.pool.accounts))

    def test_queue_scope_v2_and_v3_normalize_to_same_endpoint(self):
        account_obj = self.accounts[0]
        plain = Q.queue_scope(account_obj, MODEL)
        for version in ("v2", "v3"):
            scoped = Q.queue_scope(account_obj, MODEL,
                plain[3] + "/" + version + "/chat/completions",
                product=account_obj.product)
            self.assertEqual(scoped, plain)

    def test_probe_account_queue_and_recovery(self):
        a = self.accounts[0]
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"),
                                self.queue_error(6020, 40))]
        with mock.patch.object(P, "read_http_error",
                                side_effect=lambda exc, n: self.queue_error(6020, 40).encode()):
            row = P.probe_account(a, MODEL)
        self.assertTrue(row["model_queue"])
        self.assertEqual(row["status"], 429)
        self.assertEqual(a.model_cooldowns, {})
        blocked = P.probe_account(a, MODEL)
        self.assertTrue(blocked["model_queue"])
        self.assertEqual(len(self.calls), 1)
        P.MODEL_QUEUE_GATE.states[Q.queue_scope(a, MODEL)]["until"] = self.clock.now - 1
        self.plan = [response()]
        with mock.patch.object(P, "aggregate_stream", return_value={"choices": [{"message": {"content": "ok"}}]}):
            row = P.probe_account(a, MODEL)
        self.assertTrue(row["ok"], row)
        self.assertEqual(P.MODEL_QUEUE_GATE.snapshot(), [])

    def test_probe_scope_snapshot_survives_concurrent_switch(self):
        a = self.accounts[0]
        old_scope = Q.queue_scope(a, MODEL)
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"),
                                self.queue_error(6021, 19))]
        original = Q.queue_scope
        def switch_after_transport_url(account_obj, model, chat_url=None, product=None):
            if chat_url is not None and a.product == "workbuddy":
                a.set_product("vscode")
            return original(account_obj, model, chat_url, product=product)
        with mock.patch.object(Q, "queue_scope", side_effect=switch_after_transport_url):
            row = P.probe_account(a, MODEL)
        self.assertTrue(row["model_queue"], row)
        self.assertTrue(self.calls[0].full_url.startswith(old_scope[3]))
        self.assertFalse(P.MODEL_QUEUE_GATE.check(old_scope)[0])
        self.assertTrue(P.MODEL_QUEUE_GATE.check(Q.queue_scope(a, MODEL))[0])
        self.assertEqual(P.MODEL_QUEUE_GATE.snapshot()[0]["product"], "workbuddy")

    def test_wait_heartbeat_wake_all_and_shared_request_budget(self):
        gate = P.MODEL_QUEUE_GATE
        scope = Q.queue_scope(self.accounts[0], MODEL)
        gate.note(scope, 30)
        gate.states[scope]["until"] = self.clock.now - 1
        proceed, _hint, ticket = gate.begin(scope)
        self.assertTrue(proceed)
        self.assertTrue(gate.heartbeat(scope, ticket, 300))
        self.assertEqual(gate.probes[scope]["deadline"], self.clock.now + 300)
        self.clock.now += 181
        self.assertEqual(gate.begin(scope)[:2], (False, 1))
        self.assertFalse(gate.heartbeat(scope, (ticket[0], False), 300))
        gate.abandon(scope, ticket)
        self.assertEqual(gate.probes, {})

        gate.note(scope, 30)
        outcome = []
        def wait_for_stop():
            try:
                outcome.append(gate.wait(scope, 10,
                                         cancelled=P._client_disconnected))
            except Q.QueueWaitCancelled:
                outcome.append("cancelled")
        waiter = threading.Thread(target=wait_for_stop)
        with mock.patch.object(P._REQUEST_STOP, "is_set", return_value=True), \
             mock.patch.object(P, "_client_disconnected", lambda: True):
            waiter.start()
            waiter.join(4)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(gate.waiters, {})
        self.assertEqual(outcome, ["cancelled"])

        with mock.patch.object(P.wb_settings, "model_queue_retry", return_value={
                "model_queue_retry_attempts": 2,
                "model_queue_wait_budget_seconds": 30}), \
             mock.patch.object(P.MODEL_QUEUE_GATE, "wait", return_value=False), \
             mock.patch.object(P, "_pause_request_slots"), \
             mock.patch.object(P, "_resume_request_slots"):
            budget = P._new_queue_retry_state()
            started = time.monotonic()
            while time.monotonic() - started < 0.015:
                pass
            P._REQUEST_CANCEL.queue_retry = budget
            self.addCleanup(setattr, P._REQUEST_CANCEL, "queue_retry", None)
            for _realm in ("cn", "intl"):
                _exc = P.ModelQueueLimited("synthetic busy", scope=scope)
                with mock.patch.object(P, "_open_upstream_attempts", side_effect=_exc):
                    with self.assertRaises(P.ModelQueueLimited):
                        P.open_upstream(self.payload(), target_realm=_realm)
            self.assertGreater(budget["started"], 0)
            self.assertEqual(budget["retried"], 0)
            self.assertEqual(self.calls, [])

    def test_keepalive_requests_own_independent_chat_slots(self):
        original = P._REQUEST_CANCEL
        P._REQUEST_CANCEL = threading.local()
        P._REQUEST_CANCEL.state = [threading.Event(), None, None]
        P._REQUEST_CANCEL.chat_slot_paused = True
        self.addCleanup(setattr, P, "_REQUEST_CANCEL", original)
        slots = threading.BoundedSemaphore(1)
        with mock.patch.object(P, "_chat_slots", slots):
            for _ in range(3):
                P._REQUEST_CANCEL.chat_slot_paused = False
                self.assertTrue(slots.acquire(blocking=False))
                P._release_request_chat_slot()
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()

    def test_cancellation_after_slot_reacquisition_never_calls_upstream(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        exc = P.ModelQueueLimited("synthetic busy", scope=scope)
        P._REQUEST_CANCEL.state = [threading.Event(), None, None]
        P._REQUEST_CANCEL.chat_slot_paused = False
        self.addCleanup(setattr, P._REQUEST_CANCEL, "state", None)
        self.addCleanup(setattr, P._REQUEST_CANCEL, "chat_slot_paused", False)
        slots = threading.BoundedSemaphore(1)
        self.assertTrue(slots.acquire(blocking=False))
        def cancelled():
            # Exactly the post-resume check, after a real reacquisition.
            return P._REQUEST_CANCEL.chat_slot_paused is False
        with mock.patch.object(P.wb_settings, "model_queue_retry", return_value={
                "model_queue_retry_attempts": 1,
                "model_queue_wait_budget_seconds": 1}), \
             mock.patch.object(P.MODEL_QUEUE_GATE, "wait", return_value=True), \
             mock.patch.object(P, "_client_disconnected", side_effect=cancelled), \
             mock.patch.object(P, "_chat_slots", slots), \
             mock.patch.object(P, "_open_upstream_attempts", side_effect=exc):
            with self.assertRaises(Q.QueueWaitCancelled):
                self.call()
            self.assertEqual(self.calls, [])
            self.assertFalse(P._REQUEST_CANCEL.chat_slot_paused)
            P._release_request_chat_slot()
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()

    def test_slot_restoration_is_bounded_and_does_not_mask_queue_error(self):
        scope = Q.queue_scope(self.accounts[0], MODEL)
        exc = P.ModelQueueLimited("synthetic busy", scope=scope)
        gate_wait = mock.patch.object(P.MODEL_QUEUE_GATE, "wait", return_value=True)
        # The real global semaphore is occupied by an unrelated model. Queue
        # waiting must yield this request's slot, not add another 30s wait or
        # turn a real upstream 429 into QueueWaitCancelled / a silent close.
        P._chat_slots.acquire()
        self.addCleanup(P._chat_slots.release)
        settings = mock.patch.object(P.wb_settings, "model_queue_retry", return_value={
            "model_queue_retry_attempts": 1,
            "model_queue_wait_budget_seconds": 1})
        P._REQUEST_CANCEL.state = [threading.Event(), None, None]
        self.addCleanup(setattr, P._REQUEST_CANCEL, "state", None)
        self.addCleanup(setattr, P._REQUEST_CANCEL, "chat_slot_paused", False)
        with gate_wait, settings, mock.patch.object(P, "CHAT_SLOT_WAIT_SECONDS", 30), \
             mock.patch.object(P, "_client_disconnected", return_value=False), \
             mock.patch.object(P, "_open_upstream_attempts", side_effect=exc):
            started = time.monotonic()
            with self.assertRaises(P.ModelQueueLimited) as raised:
                P.open_upstream(self.payload(), target_realm="cn")
            self.assertIs(raised.exception, exc)
            self.assertLess(time.monotonic() - started, 5)
            if P._REQUEST_CANCEL.chat_slot_paused:
                P._release_request_chat_slot()
                self.assertFalse(P._REQUEST_CANCEL.chat_slot_paused)
        self.assertEqual(self.calls, [])

    def test_settings_validate_merge_and_survive_save(self):
        defaults = wb_settings.model_queue_retry(self.tmp.name)
        self.assertEqual(defaults, {
            "model_queue_retry_attempts": 0,
            "model_queue_wait_budget_seconds": 30})
        # A syntactically valid but huge stored integer must fail closed to
        # defaults, not turn a real queue response into a local OverflowError.
        wb_settings.save(self.tmp.name, {
            "model_queue_retry_attempts": 10 ** 400,
            "model_queue_wait_budget_seconds": 10 ** 400})
        self.assertEqual(wb_settings.model_queue_retry(self.tmp.name), defaults)
        stored = wb_settings.set_model_queue_retry(self.tmp.name, {
            "model_queue_retry_attempts": 2})
        self.assertEqual(stored, {
            "model_queue_retry_attempts": 2,
            "model_queue_wait_budget_seconds": 30})
        self.assertEqual(wb_settings.model_queue_retry(self.tmp.name), stored)
        for invalid in (None, [], {"model_queue_retry_attempts": True},
                        {"model_queue_retry_attempts": 9},
                        {"model_queue_wait_budget_seconds": 121},
                        {"model_queue_wait_budget_seconds": "30.5"}):
            with self.assertRaises(ValueError):
                wb_settings.validate_model_queue_retry_patch(invalid)
        self.assertEqual(wb_settings.validate_model_queue_retry_patch({
            "model_queue_retry_attempts": "2",
            "model_queue_wait_budget_seconds": 30.0}), {
                "model_queue_retry_attempts": 2,
                "model_queue_wait_budget_seconds": 30})
        class SaveRequest:
            _handle_settings_save = P.Handler._handle_settings_save
            def __init__(self, payload): self.payload, self.answer = payload, []
            def _payload_or_error(self, allow_list=False): return self.payload
            def _json(self, code, body): self.answer.append((code, body))
            def _error(self, code, message, kind):
                self.answer.append((code, {"error": message}))
        with mock.patch.object(P, "ACCOUNTS_DIR", self.tmp.name), \
             mock.patch.object(P, "apply_log_db_settings", lambda: None):
            request = SaveRequest({})
            request._handle_settings_save()
            self.assertEqual(request.answer[0][0], 200)
            self.assertEqual(wb_settings.model_queue_retry(self.tmp.name), stored)
            request = SaveRequest({"model_queue_retry": {
                "model_queue_retry_attempts": 9}})
            request._handle_settings_save()
            self.assertEqual(request.answer[0][0], 400)
            self.assertEqual(wb_settings.model_queue_retry(self.tmp.name), stored)
            request = SaveRequest({"model_queue_retry": {
                "model_queue_wait_budget_seconds": 30}})
            request._handle_settings_save()
            self.assertEqual(request.answer[0][0], 200)
            self.assertEqual(wb_settings.model_queue_retry(self.tmp.name), {
                "model_queue_retry_attempts": 2,
                "model_queue_wait_budget_seconds": 30})
        with mock.patch.object(P, "ACCOUNTS_DIR", self.tmp.name):
            self.assertEqual(P.runtime_settings_view()["model_queue_retry"], stored)

    def test_probe_large_queue_json_keeps_shared_scope(self):
        a = self.accounts[0]
        body = json.dumps({"padding": "x" * (1024 * 1024), "code": 6020,
                           "data": {"retry_after": 40}})
        self.plan = [http_error(types.SimpleNamespace(full_url="https://edge.invalid"), body)]
        row = P.probe_account(a, MODEL)
        self.assertTrue(row["model_queue"], row)
        self.assertEqual(row["queue_code"], 6020)
        self.assertEqual(row["retry_after"], 40)
        self.assertEqual(a.model_cooldowns, {})
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
