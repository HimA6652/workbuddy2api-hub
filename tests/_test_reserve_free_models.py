"""A positive reserve must not park a model the realm catalogue marks free.

The low-credit guard protects a paid balance. A model the catalogue prices at
"x0.00" does not spend that balance, so a model-blind guard also takes down
free capacity. The default reserve=0, paid/unknown models, unknown balances,
and the account-level public reserve state must stay unchanged.

No network: tests inject the already-existing realm free-model view.

    python tests/_test_reserve_free_models.py
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _isolated_dirs import isolated_data_dirs    # noqa: E402

_TMP = isolated_data_dirs("wb-reserve-free-")

import wb_accounts      # noqa: E402
import wb_settings      # noqa: E402
import wb_proxy as P    # noqa: E402

FREE = "deepseek-v4.1-flash"
PAID = "gpt-6-astra"
UNKNOWN = "brand-new-model"


def account(uid, remain, realm="intl", free=()):
    entry = wb_accounts.Account({
        "uid": uid,
        "accessToken": "token-%s" % uid,
        "realm": realm,
        "credits": {"remain": remain, "used": 0, "size": 100},
    })
    if free:
        entry.free_models = frozenset(free)
    return entry


def reserve_pool(directory, accounts, values=1, free_models=None):
    pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
    pool.accounts = accounts
    pool.apply_reserve_credits(values, free_models)
    return pool


class AccountReserveFreeModelTests(unittest.TestCase):
    def test_zero_balance_serves_a_free_model_only(self):
        acc = account("uid-free", 0, free={FREE})
        acc.reserve_credits = 1
        self.assertTrue(acc.ready(FREE))
        self.assertEqual(acc.unavailable_reason(FREE), "")
        self.assertFalse(acc.ready(PAID))
        self.assertFalse(acc.ready(UNKNOWN))
        self.assertEqual(acc.unavailable_reason(PAID), "余额低于保留线")
        self.assertEqual(acc.unavailable_reason(UNKNOWN), "余额低于保留线")
        self.assertFalse(acc.ready())
        self.assertEqual(acc.unavailable_reason(), "余额低于保留线")
        # The account-level reserve fact remains visible while this model is exempt.
        self.assertTrue(acc.reserve_blocked())
        self.assertTrue(acc.public()["reserveBlocked"])
        self.assertFalse(acc.reserve_blocked(FREE))

    def test_remaining_below_the_line_serves_free_too(self):
        acc = account("uid-low", 1, free={FREE})
        acc.reserve_credits = 10
        self.assertTrue(acc.ready(FREE))
        self.assertFalse(acc.ready(PAID))
        acc.credits["remain"] = 10
        self.assertTrue(acc.reserve_blocked())
        self.assertTrue(acc.ready(FREE))
        self.assertFalse(acc.ready(PAID))

    def test_reason_and_ready_agree_for_every_model(self):
        acc = account("uid-agree", 0, free={FREE})
        acc.reserve_credits = 1
        for model in (FREE, PAID, UNKNOWN, None):
            reason = acc.unavailable_reason(model)
            if reason:
                self.assertFalse(acc.ready(model), model)
            else:
                self.assertTrue(acc.ready(model), model)

    def test_default_reserve_stays_off(self):
        acc = account("uid-default", 0, free={FREE})
        self.assertEqual(acc.reserve_credits, 0)
        self.assertFalse(acc.reserve_blocked())
        self.assertTrue(acc.ready())
        self.assertTrue(acc.ready(PAID))
        self.assertFalse(acc.public()["reserveBlocked"])

    def test_missing_catalogue_fails_closed(self):
        acc = account("uid-empty-table", 0)
        acc.reserve_credits = 1
        self.assertEqual(acc.free_models, frozenset())
        self.assertFalse(acc.ready(FREE))
        self.assertTrue(acc.reserve_blocked(FREE))
        acc.free_models = None
        self.assertFalse(acc.ready(FREE))
        self.assertTrue(acc.reserve_blocked(FREE))

    def test_unknown_balance_stays_usable(self):
        acc = wb_accounts.Account({"uid": "uid-unknown-balance",
                                   "accessToken": "t", "realm": "intl"})
        acc.reserve_credits = 10
        self.assertFalse(acc.reserve_blocked())
        self.assertTrue(acc.ready(PAID))


class PoolReserveFreeModelTests(unittest.TestCase):
    def test_free_ids_are_per_realm(self):
        with tempfile.TemporaryDirectory(prefix="reserve-free-realm-") as directory:
            intl = account("uid-intl", 0, realm="intl")
            cn = account("uid-cn", 0, realm="cn")
            pool = reserve_pool(directory, [intl, cn], 1,
                                {"intl": {FREE}, "cn": set()})
            self.assertEqual(intl.free_models, frozenset({FREE}))
            self.assertEqual(cn.free_models, frozenset())
            self.assertTrue(intl.ready(FREE))
            # This id is x0.00 on intl and x0.11 on cn in the bundled catalogue.
            self.assertTrue(cn.reserve_blocked(FREE))
            self.assertFalse(cn.ready(FREE))
            self.assertEqual(pool.count_ready(realm="intl", model=FREE), 1)
            self.assertEqual(pool.count_ready(realm="cn", model=FREE), 0)
            self.assertEqual(pool.count_ready(realm="cn", model=PAID), 0)

    def test_pick_hands_the_free_model_to_the_empty_account(self):
        with tempfile.TemporaryDirectory(prefix="reserve-free-pick-") as directory:
            empty = account("uid-empty", 0, free={FREE})
            rich = account("uid-rich", 100, free={FREE})
            pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            pool.accounts = [empty, rich]
            pool.apply_reserve_credits(1)
            for _ in range(4):
                self.assertEqual(pool.pick(realm="intl", model=PAID).uid,
                                 "uid-rich")
            self.assertEqual(pool.count_ready(realm="intl", model=PAID), 1)
            disabled = account("uid-disabled", 0, free={FREE})
            disabled.enabled = False
            free_pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            free_pool.accounts = [empty, disabled]
            free_pool.apply_reserve_credits(1)
            picked = free_pool.pick(realm="intl", model=FREE)
            self.assertIsNotNone(picked, "free model must reach the parked account")
            self.assertEqual(picked.uid, "uid-empty")
            self.assertEqual(free_pool.count_ready(realm="intl", model=FREE), 1)
            self.assertEqual(free_pool.count_ready(realm="intl", model=PAID), 0)
            row = [a for a in free_pool.list_public(realm="intl")
                   if a["uid"] == "uid-empty"][0]
            self.assertTrue(row["reserveBlocked"])
            self.assertEqual(row["reserveCredits"], 1)

    def test_none_keeps_the_last_known_free_table(self):
        with tempfile.TemporaryDirectory(prefix="reserve-free-keep-") as directory:
            acc = account("uid-keep", 0)
            pool = reserve_pool(directory, [acc], 1,
                                {"intl": {FREE}, "cn": set()})
            self.assertTrue(acc.ready(FREE))
            pool.apply_reserve_credits(1)
            self.assertEqual(acc.free_models, frozenset({FREE}))
            self.assertTrue(acc.ready(FREE))
            pool.apply_reserve_credits(0)
            self.assertFalse(acc.reserve_blocked())
            self.assertTrue(acc.ready(PAID))

    def test_missing_free_table_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="reserve-free-missing-") as directory:
            acc = account("uid-missing", 0)
            pool = reserve_pool(directory, [acc], 1, {})
            self.assertEqual(acc.free_models, frozenset())
            self.assertTrue(acc.reserve_blocked(FREE))
            self.assertFalse(acc.ready(FREE))
            self.assertEqual(pool.count_ready(realm="intl", model=FREE), 0)


class ProxyWiringTests(unittest.TestCase):
    """The pool only learns the catalogue view from the proxy."""

    class _Pool(object):
        def __init__(self, calls):
            self.calls = calls

        def apply_reserve_credits(self, values=None, free_models=None):
            self.calls.append((dict(values or {}), free_models))
            return values

    def setUp(self):
        self.old_pool = P.POOL
        self.old_dir = P.ACCOUNTS_DIR
        self.calls = []
        P.POOL = self._Pool(self.calls)
        P.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]
        wb_settings.set_limit(P.ACCOUNTS_DIR, "reserve_credits", "global", 0)
        self.addCleanup(self._restore)

    def _restore(self):
        wb_settings.set_limit(P.ACCOUNTS_DIR, "reserve_credits", "global", 0)
        P.POOL = self.old_pool
        P.ACCOUNTS_DIR = self.old_dir

    def test_positive_reserve_publishes_the_realm_free_table(self):
        wb_settings.set_limit(P.ACCOUNTS_DIR, "reserve_credits", "global", 5)
        reads = []
        view = {"intl": {FREE}, "cn": set()}

        def counting():
            reads.append(1)
            return view

        with mock.patch.object(P, "free_models_by_realm", counting):
            P.apply_reserve_credits()
        self.assertEqual(len(reads), 1, "positive reserve must read the table")
        values, free_models = self.calls[-1]
        self.assertEqual(values["global"], 5)
        self.assertEqual(free_models, view)

    def test_reserve_zero_does_not_read_the_free_table(self):
        reads = []

        def counting():
            reads.append(1)
            return {"intl": {FREE}, "cn": set()}

        with mock.patch.object(P, "free_models_by_realm", counting):
            P.apply_reserve_credits()
        self.assertEqual(reads, [], "reserve=0 must not read the catalogue view")
        self.assertEqual(self.calls, [], "reserve=0 must not touch the pool")

    def test_settings_save_publish_clears_a_switched_off_guard(self):
        reads = []
        with mock.patch.object(P, "free_models_by_realm",
                               lambda: reads.append(1) or {}):
            P.apply_reserve_credits(refresh=True)
        self.assertEqual(reads, [])
        self.assertEqual(self.calls[-1][0]["global"], 0)
        self.assertIsNone(self.calls[-1][1])

    def test_dispatch_refreshes_the_guard(self):
        wb_settings.set_limit(P.ACCOUNTS_DIR, "reserve_credits", "global", 5)
        pool = wb_accounts.AccountPool(P.ACCOUNTS_DIR, log=lambda _m: None)
        parked = account("uid-parked", 0)
        parked.enabled = False
        pool.accounts = [parked]
        P.POOL = pool
        with mock.patch.object(P, "apply_reserve_credits",
                               wraps=P.apply_reserve_credits) as spy:
            with self.assertRaises(RuntimeError):
                P.open_upstream({"model": FREE,
                                 "messages": [{"role": "user", "content": "hi"}]},
                                target_realm="intl")
        self.assertEqual(spy.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
