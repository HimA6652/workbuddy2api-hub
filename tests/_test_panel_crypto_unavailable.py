"""Panel password crypto failure handling (issue #658).

When an environment lacks hashlib.pbkdf2_hmac (e.g. Windows Smart App Control
blocking DLLs/_hashlib.pyd in portable Python), panel login previously swallowed
the exception and returned 401 "invalid panel password", masquerading an environment
failure as a bad password.

This suite verifies:
  1. /panel/login returns HTTP 500 server_error with actionable explanation
     instead of 401 invalid panel password;
  2. Crypto errors do not burn the brute-force login attempt quota;
  3. /panel/password returns HTTP 500 when changing passwords without pbkdf2;
  4. /health and /panel/status reflect crypto_ok status.

    python tests/_test_panel_crypto_unavailable.py
"""
import email
import hashlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _isolated_dirs import isolated_data_dirs

_TMP = isolated_data_dirs("wb-panel-crypto-")

import wb_proxy as proxy
import wb_settings as settings


class RequestDispatch(proxy.Handler):
    def __init__(self, path, command="POST", body=b"", headers=None, client=("127.0.0.1", 40000)):
        self.path = path
        self.command = command
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self.client_address = client
        merged = {"Content-Length": str(len(body))}
        merged.update(headers or {})
        raw = "".join("%s: %s\r\n" % item for item in merged.items())
        self.headers = email.message_from_string(raw)
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_body = None

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        self.status = status
        self.response_body = payload
        return status, payload

    def _error(self, status, message, err_type="invalid_request_error"):
        self.status = status
        self.response_body = {"error": {"message": message, "type": err_type}}
        return status, self.response_body


class PanelCryptoUnavailableTests(unittest.TestCase):
    def setUp(self):
        proxy.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]
        settings.set_panel_password(proxy.ACCOUNTS_DIR, "secret123")
        with proxy._login_lock:
            proxy._login_attempts.clear()

    def test_health_and_panel_status_report_crypto_ok_when_healthy(self):
        req = RequestDispatch("/health", "GET")
        req._get_health()
        self.assertEqual(200, req.status)
        self.assertTrue(req.response_body.get("crypto_ok"))

        req_panel = RequestDispatch("/panel/status", "GET")
        req_panel._get_panel_status()
        self.assertEqual(200, req_panel.status)
        self.assertTrue(req_panel.response_body.get("crypto_ok"))

    def test_health_and_panel_status_report_crypto_error_when_missing(self):
        with mock.patch.object(settings, "check_pbkdf2_available",
                               return_value=(False, "no attribute 'pbkdf2_hmac'")):
            req = RequestDispatch("/health", "GET")
            req._get_health()
            self.assertEqual(200, req.status)
            self.assertFalse(req.response_body.get("crypto_ok"))
            self.assertIn("pbkdf2_hmac", req.response_body.get("crypto_error", ""))

            req_panel = RequestDispatch("/panel/status", "GET")
            req_panel._get_panel_status()
            self.assertEqual(200, req_panel.status)
            self.assertFalse(req_panel.response_body.get("crypto_ok"))
            self.assertIn("pbkdf2_hmac", req_panel.response_body.get("crypto_error", ""))

    def test_login_returns_500_with_actionable_message_when_pbkdf2_missing(self):
        body = json.dumps({"password": "secret123"}).encode("utf-8")
        req = RequestDispatch("/panel/login", "POST", body=body)

        with mock.patch.object(hashlib, "pbkdf2_hmac",
                               side_effect=AttributeError("no attribute 'pbkdf2_hmac'")):
            req.do_POST()

        self.assertEqual(500, req.status)
        err = req.response_body.get("error", {})
        self.assertEqual("server_error", err.get("type"))
        self.assertIn("hashlib.pbkdf2_hmac", err.get("message", ""))
        # Attempts quota must NOT be incremented for environment failure
        self.assertEqual([], proxy._login_attempts.get("127.0.0.1", []))

    def test_login_wrong_password_still_returns_401_when_crypto_available(self):
        body = json.dumps({"password": "wrong_password"}).encode("utf-8")
        req = RequestDispatch("/panel/login", "POST", body=body)
        req.do_POST()

        self.assertEqual(401, req.status)
        err = req.response_body.get("error", {})
        self.assertEqual("invalid_request_error", err.get("type"))
        self.assertEqual("invalid panel password", err.get("message"))
        self.assertIn("127.0.0.1", proxy._login_attempts)

    def test_change_password_returns_500_when_pbkdf2_missing(self):
        body = json.dumps({"current": "secret123", "new": "newpass456"}).encode("utf-8")
        req = RequestDispatch("/panel/password", "POST", body=body)

        with mock.patch.object(proxy.Handler, "_panel_ok", return_value=True):
            with mock.patch.object(hashlib, "pbkdf2_hmac",
                                   side_effect=AttributeError("no attribute 'pbkdf2_hmac'")):
                req.do_POST()

        self.assertEqual(500, req.status)
        err = req.response_body.get("error", {})
        self.assertEqual("server_error", err.get("type"))
        self.assertIn("hashlib.pbkdf2_hmac", err.get("message", ""))


if __name__ == "__main__":
    unittest.main()
