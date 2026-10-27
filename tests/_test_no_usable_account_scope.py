"""The pool-empty error must name the candidates, and say "waiting" when it is.

A pool-empty failure has two very different shapes, and the gateway used
to report them the same way:

  * the candidates are unusable for a reason the operator must fix
    (disabled, expired, locally parked) -> 503, "add or enable an account";
  * every candidate is only waiting for the upstream window of one model
    to reset -> the pool is idle, not broken. The right answer is 429 with a
    Retry-After, otherwise the client retries every few milliseconds
    (measured: 12 times in three minutes on a real deployment).

The error text also listed the accounts of the whole realm instead of the
ones this API key may use, which sent the reader after the wrong accounts
(issue #665).

    python tests/_test_no_usable_account_scope.py
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _isolated_dirs import isolated_data_dirs    # noqa: E402

_TMP = isolated_data_dirs("wb-no-usable-account-")

import wb_accounts as A        # noqa: E402
import wb_proxy as P           # noqa: E402


MODEL = "deepseek-v4.1-flash"


def account(uid="aaaa1111-bbbb-cccc-dddd-eeeeffff0000", realm="intl", **overrides):
    data = {"uid": uid, "accessToken": "x.y.z", "nickname": uid[:8],
            "enabled": True, "realm": realm}
    data.update(overrides)
    return A.Account(data)


def payload():
    return {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}


class DetailScopeTests(unittest.TestCase):
    """The per-account detail lists the candidates, not the realm."""

    def setUp(self):
        self.original = P.POOL
        self.pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])

    def tearDown(self):
        P.POOL = self.original

    def _cooling(self, uid, realm="cn"):
        acc = account(uid=uid, realm=realm)
        acc.model_cooldowns[MODEL] = time.time() + 600
        return acc

    def test_only_the_candidates_are_listed(self):
        P.POOL = self.pool
        a = self._cooling("cn000001-0000-0000-0000-000000000000")
        b = self._cooling("cn000002-0000-0000-0000-000000000000")
        self.pool.accounts = [a, b]
        detail = P.pool_unavailable_detail("cn", model=MODEL,
                                            allowed_accounts={a.uid})
        self.assertIn("cn000001", detail)
        self.assertNotIn("cn000002", detail)

    def test_without_a_binding_the_realm_is_still_listed(self):
        P.POOL = self.pool
        a = self._cooling("cn000001-0000-0000-0000-000000000000")
        b = self._cooling("cn000002-0000-0000-0000-000000000000")
        self.pool.accounts = [a, b]
        detail = P.pool_unavailable_detail("cn", model=MODEL)
        self.assertIn("cn000001", detail)
        self.assertIn("cn000002", detail)

    def test_a_binding_outside_the_realm_says_so(self):
        P.POOL = self.pool
        a = self._cooling("intl0001-0000-0000-0000-000000000000", realm="intl")
        self.pool.accounts = [a]
        detail = P.pool_unavailable_detail("cn", model=MODEL,
                                            allowed_accounts={a.uid})
        self.assertIn("cn", detail)

class ErrorScopeTests(unittest.TestCase):
    """The whole path: what the client is told, and with which status."""

    def setUp(self):
        self.original = P.POOL
        self.pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])

    def tearDown(self):
        P.POOL = self.original

    def test_the_error_names_the_binding(self):
        P.POOL = self.pool
        a = account(uid="cn000001-0000-0000-0000-000000000000",
                     realm="cn", enabled=False)
        b = account(uid="cn000002-0000-0000-0000-000000000000",
                     realm="cn", enabled=False)
        self.pool.accounts = [a, b]
        with self.assertRaises(RuntimeError) as ctx:
            P.open_upstream(payload(), target_realm="cn",
                           allowed_accounts={a.uid})
        message = str(ctx.exception)
        # The prefix stays: gateway_hint classifies on it.
        self.assertIn("no usable account for realm", message)
        self.assertIn("this API key may use 1 account(s)", message)
        self.assertIn("cn000001", message)
        self.assertNotIn("cn000002", message)
        self.assertIn("no healthy account", P.gateway_hint(503, message))

    def test_whole_realm_cooling_is_429(self):
        """Every account of the realm cools the model: wait, do not fail."""
        P.POOL = self.pool
        acc = account(uid="cn000001-0000-0000-0000-000000000000",
                     realm="cn")
        acc.model_cooldowns[MODEL] = time.time() + 600
        self.pool.accounts = [acc]
        with self.assertRaises(P.RateLimited) as ctx:
            P.open_upstream(payload(), target_realm="cn")
        exc = ctx.exception
        self.assertGreater(exc.wait, 0)
        self.assertLessEqual(exc.wait, 600)
        self.assertIn(MODEL, exc.message)
        self.assertIn("cooling", exc.message)
        self.assertIn("rate limited", P.gateway_hint(429, exc.message))

    def test_bound_candidates_cooling_is_429(self):
        """The realm still has healthy accounts, but not for this key."""
        P.POOL = self.pool
        bound = account(uid="cn000001-0000-0000-0000-000000000000",
                     realm="cn")
        bound.model_cooldowns[MODEL] = time.time() + 600
        other = account(uid="cn000002-0000-0000-0000-000000000000",
                      realm="cn")
        self.pool.accounts = [bound, other]
        with self.assertRaises(P.RateLimited) as ctx:
            P.open_upstream(payload(), target_realm="cn",
                           allowed_accounts={bound.uid})
        exc = ctx.exception
        self.assertIn("waiting for the upstream", exc.message)
        self.assertIn("1 account(s)", exc.message)
        self.assertLessEqual(exc.wait, 600)

    def test_a_broken_candidate_is_not_a_wait(self):
        """One candidate needing a re-login is enough to keep this a 503."""
        P.POOL = self.pool
        cooling = account(uid="cn000001-0000-0000-0000-000000000000",
                       realm="cn")
        cooling.model_cooldowns[MODEL] = time.time() + 600
        broken = account(uid="cn000002-0000-0000-0000-000000000000",
                       realm="cn")
        broken.token_expired = True
        self.pool.accounts = [cooling, broken]
        with self.assertRaises(RuntimeError):
            P.open_upstream(payload(), target_realm="cn")


class BlockedByCooldownTests(unittest.TestCase):
    """The predicate behind the 429: nothing but the model window is wrong."""

    def test_a_cooling_account_is_blocked_by_the_cooldown(self):
        acc = account()
        acc.model_cooldowns[MODEL] = time.time() + 600
        self.assertTrue(acc.blocked_by_model_cooldown(MODEL))

    def test_a_healthy_account_is_not(self):
        acc = account()
        self.assertFalse(acc.blocked_by_model_cooldown(MODEL))

    def test_an_expired_credential_is_not_just_a_wait(self):
        acc = account()
        acc.model_cooldowns[MODEL] = time.time() + 600
        acc.token_expired = True
        self.assertFalse(acc.blocked_by_model_cooldown(MODEL))

    def test_a_local_guard_is_not_just_a_wait(self):
        acc = account()
        acc.model_cooldowns[MODEL] = time.time() + 600
        acc.daily_token_limit = 1000
        acc.daily_tokens_today = 1000
        self.assertFalse(acc.blocked_by_model_cooldown(MODEL))

    def test_no_model_means_no_wait(self):
        acc = account()
        acc.model_cooldowns[MODEL] = time.time() + 600
        self.assertFalse(acc.blocked_by_model_cooldown(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)