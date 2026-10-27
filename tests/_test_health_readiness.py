"""The health endpoint must say which accounts can actually serve.

`/health` used to report two numbers: how many accounts the pool holds and
how many of them are "ready" - counted across every realm and without
looking at model-level cooldowns. An account parked by an upstream per-model
quota cap still counted as ready, so the probe and the panel stayed green
while the pool was empty for that model (issue #667).

This suite pins three things:

  * the per-realm counts (`accounts_by_realm` / `accounts_ready_by_realm`);
  * the per-model counts (`models_ready`), which is where a model-level
    cooldown shows up;
  * that the per-model count is read-only: it must not trigger a token
    refresh, because a probe asking "how many accounts are left" must not
    turn into a network call per model.

    python tests/_test_health_readiness.py
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _isolated_dirs import isolated_data_dirs    # noqa: E402

_TMP = isolated_data_dirs("wb-health-readiness-")

import wb_accounts as A        # noqa: E402
import wb_proxy as P           # noqa: E402


def account(uid="aaaa1111-bbbb-cccc-dddd-eeeeffff0000", realm="intl", **overrides):
    data = {"uid": uid, "accessToken": "x.y.z", "nickname": uid[:8],
            "enabled": True, "realm": realm}
    data.update(overrides)
    return A.Account(data)


class PoolReadinessTests(unittest.TestCase):
    def setUp(self):
        self.original = P.POOL
        self.pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])

    def tearDown(self):
        P.POOL = self.original

    def _pool(self):
        cn_capped = account(uid="cn000001-0000-0000-0000-000000000000",
                            realm="cn")
        cn_capped.model_cooldowns["deepseek-v4.1-flash"] = time.time() + 3600
        cn_free = account(uid="cn000002-0000-0000-0000-000000000000",
                           realm="cn")
        intl = account(uid="intl0001-0000-0000-0000-000000000000")
        self.pool.accounts = [cn_capped, cn_free, intl]
        P.POOL = self.pool
        return self.pool

    def test_totals_keep_their_old_meaning(self):
        self._pool()
        out = P.pool_readiness()
        self.assertEqual(3, out["accounts"])
        # The capped account is still healthy for every other model.
        self.assertEqual(3, out["accounts_ready"])

    def test_counts_are_split_by_realm(self):
        self._pool()
        out = P.pool_readiness()
        self.assertEqual({"cn": 2, "intl": 1}, out["accounts_by_realm"])
        self.assertEqual({"cn": 2, "intl": 1}, out["accounts_ready_by_realm"])

    def test_per_model_counts_spot_the_cooldown(self):
        self._pool()
        out = P.pool_readiness()
        per_model = out["models_ready"]
        self.assertIn("deepseek-v4.1-flash", per_model)
        # One of the two CN accounts is parked for this model; the other is not.
        self.assertEqual(1, per_model["deepseek-v4.1-flash"]["cn"])
        self.assertEqual(1, per_model["deepseek-v4.1-flash"]["intl"])
        # ... while the account-level count still says both CN accounts are up.
        self.assertEqual(2, out["accounts_ready_by_realm"]["cn"])

    def test_current_realm_count_follows_the_panel_switch(self):
        self._pool()
        original = P.CURRENT_REALM
        try:
            P.CURRENT_REALM = "cn"
            self.assertEqual(2, P.pool_readiness()["accounts_ready_current_realm"])
            P.CURRENT_REALM = "intl"
            self.assertEqual(1, P.pool_readiness()["accounts_ready_current_realm"])
        finally:
            P.CURRENT_REALM = original

    def test_no_pool_is_all_zeroes(self):
        P.POOL = None
        out = P.pool_readiness()
        self.assertEqual(0, out["accounts"])
        self.assertEqual(0, out["accounts_ready"])
        self.assertEqual({}, out["models_ready"])


class KeyReadinessTests(unittest.TestCase):
    """A key can be unusable while the pool looks healthy."""

    def setUp(self):
        self.original = P.POOL
        self.pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])

    def tearDown(self):
        P.POOL = self.original

    def test_bound_accounts_are_counted(self):
        a = account(uid="cn000001-0000-0000-0000-000000000000", realm="cn")
        b = account(uid="cn000002-0000-0000-0000-000000000000", realm="cn")
        b.model_cooldowns["deepseek-v4.1-flash"] = time.time() + 3600
        self.pool.accounts = [a, b]
        P.POOL = self.pool
        entry = {"realm": "cn", "accounts": [a.uid, b.uid]}
        out = P.key_readiness(entry)
        self.assertEqual(2, out["key_accounts_bound"])
        # The cooled account is still healthy account-wise, so both count here;
        # the per-model view is where the cooldown shows up.
        self.assertEqual(2, out["key_accounts_ready"])

    def test_unbound_key_counts_the_realm(self):
        a = account(uid="cn000001-0000-0000-0000-000000000000", realm="cn")
        self.pool.accounts = [a]
        P.POOL = self.pool
        out = P.key_readiness({"realm": "cn"})
        self.assertEqual(1, out["key_accounts_bound"])
        self.assertEqual(1, out["key_accounts_ready"])

    def test_no_key_is_zeroes(self):
        P.POOL = self.pool
        out = P.key_readiness(None)
        self.assertEqual(0, out["key_accounts_ready"])
        self.assertEqual(0, out["key_accounts_bound"])


class NoRefreshTests(unittest.TestCase):
    """The read-only count must not talk to the upstream."""

    def test_refresh_false_does_not_refresh(self):
        acc = account()
        acc.expires_at = time.time() + 30       # about to expire
        calls = []
        acc.refresh = lambda: calls.append(1) or True
        self.assertTrue(acc.ready(refresh=False))
        self.assertEqual([], calls)
        self.assertTrue(acc.ready())
        self.assertEqual([1], calls)

    def test_count_ready_can_stay_read_only(self):
        pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])
        acc = account()
        acc.expires_at = time.time() + 30
        calls = []
        acc.refresh = lambda: calls.append(1) or True
        pool.accounts = [acc]
        self.assertEqual(1, pool.count_ready(refresh=False))
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)