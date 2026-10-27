"""The failed-request exit must decide the status code once, and log that code.

A pool-empty failure ("no usable account") answers 503: the client waits for
the upstream quota to reset, or the operator adds an account. Everything else
answers 502: the upstream path is unreachable and needs looking at.

Before #666 both call sites wrote the usage row as 502 first and only then
returned 503, so `usage/usage.jsonl` and the request log reported a
wait-class failure as an upstream fault - and any statistic over 5xx mixed
"wait" with "investigate". This suite pins that the row, the response and
the client-visible message agree.

    python tests/_test_error_status_consistency.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy


class _Stub(object):
    """Minimal stand-in for the request handler: only `_error` is used."""

    def __init__(self):
        self.status = None
        self.payload = None

    def _error(self, code, message, err_type=None):
        self.status = code
        self.payload = {"error": {"message": message, "code": code}}
        return code, self.payload


class ErrorStatusTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.original = proxy.record_error

        def _record(model, status, message, **kwargs):
            self.rows.append({"model": model, "status": status,
                              "message": message, "kwargs": kwargs})

        proxy.record_error = _record
        self.addCleanup(self._restore)

    def _restore(self):
        proxy.record_error = self.original

    def _run(self, exc):
        stub = _Stub()
        proxy.upstream_failure_response(stub, "deepseek-v4.1-flash", exc,
                                        1000.0, "key-id")
        return stub

    def test_pool_empty_is_503_in_row_and_response(self):
        exc = RuntimeError("no usable account for realm 'cn': all are disabled")
        stub = self._run(exc)
        self.assertEqual(1, len(self.rows))
        self.assertEqual(503, self.rows[0]["status"])
        self.assertEqual(503, stub.status)

    def test_upstream_unreachable_stays_502(self):
        exc = RuntimeError("Remote end closed connection without response")
        stub = self._run(exc)
        self.assertEqual(1, len(self.rows))
        self.assertEqual(502, self.rows[0]["status"])
        self.assertEqual(502, stub.status)

    def test_pool_empty_message_reaches_the_client_verbatim(self):
        exc = RuntimeError("no usable account for realm 'cn': all are disabled")
        stub = self._run(exc)
        message = stub.payload["error"]["message"]
        self.assertIn("no usable account for realm 'cn'", message)
        self.assertIn("add or enable one at the dashboard", message)
        # The upstream wording is never rewritten, only annotated.
        self.assertTrue(message.startswith("no usable account for realm 'cn'"))

    def test_row_keeps_model_key_and_elapsed_ms(self):
        exc = RuntimeError("no usable account for realm 'cn'")
        self._run(exc)
        row = self.rows[0]
        self.assertEqual("deepseek-v4.1-flash", row["model"])
        self.assertEqual("key-id", row["kwargs"].get("key"))
        elapsed = row["kwargs"].get("elapsed_ms")
        self.assertIsInstance(elapsed, int)
        self.assertGreaterEqual(elapsed, 0)

    def test_status_is_never_a_502_for_a_pool_empty_failure(self):
        # The exact regression: the row used to be written as 502 while the
        # client received 503.
        exc = RuntimeError("no usable account for realm 'intl': all are disabled")
        self._run(exc)
        self.assertNotEqual(502, self.rows[0]["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
