"""Panel password storage and verification (#523).

The reported symptom was "changed the password, admin still works". The
storage layer is what decides that, so this suite pins the round trip: the
default opens the panel, a custom password replaces it, and setting it back
to "admin" restores the default state.

    python tests/_test_panel_password.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import hashlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_settings


class PanelPasswordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="wb-panel-password-")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_a_fresh_install_opens_with_admin(self):
        self.assertTrue(wb_settings.verify_panel_password(self.directory, "admin"))
        self.assertTrue(wb_settings.panel_password_is_default(self.directory))

    def test_a_custom_password_replaces_admin(self):
        wb_settings.set_panel_password(self.directory, "secret123")
        self.assertFalse(wb_settings.verify_panel_password(self.directory, "admin"))
        self.assertTrue(wb_settings.verify_panel_password(self.directory, "secret123"))
        self.assertFalse(wb_settings.panel_password_is_default(self.directory))

    def test_the_stored_hash_is_not_the_plaintext(self):
        wb_settings.set_panel_password(self.directory, "secret123")
        data = wb_settings.load(self.directory)
        self.assertNotIn("secret123", str(data))
        self.assertTrue(data.get("panel_password_hash"))
        self.assertTrue(data.get("panel_password_salt"))

    def test_changing_the_password_changes_the_salt(self):
        wb_settings.set_panel_password(self.directory, "secret123")
        first = wb_settings.load(self.directory).get("panel_password_salt")
        wb_settings.set_panel_password(self.directory, "secret456")
        second = wb_settings.load(self.directory).get("panel_password_salt")
        self.assertNotEqual(first, second)

    def test_setting_admin_back_restores_the_default(self):
        wb_settings.set_panel_password(self.directory, "secret123")
        wb_settings.set_panel_password(self.directory, "admin")
        self.assertTrue(wb_settings.verify_panel_password(self.directory, "admin"))
        self.assertTrue(wb_settings.panel_password_is_default(self.directory))
        self.assertFalse(wb_settings.verify_panel_password(self.directory, "secret123"))

    def test_check_pbkdf2_available_normal(self):
        ok, reason = wb_settings.check_pbkdf2_available()
        self.assertTrue(ok)
        self.assertEqual("", reason)

    def test_verify_raises_explicit_crypto_error_when_pbkdf2_missing(self):
        wb_settings.set_panel_password(self.directory, "secret123")
        with mock.patch.object(hashlib, "pbkdf2_hmac", side_effect=AttributeError("no attribute 'pbkdf2_hmac'")):
            with self.assertRaises(wb_settings.PanelPasswordCryptoError) as ctx:
                wb_settings.verify_panel_password(self.directory, "secret123")
            self.assertIn("hashlib.pbkdf2_hmac", str(ctx.exception))

    def test_default_password_still_opens_even_if_pbkdf2_missing(self):
        with mock.patch.object(hashlib, "pbkdf2_hmac", side_effect=AttributeError("no attribute 'pbkdf2_hmac'")):
            self.assertTrue(wb_settings.verify_panel_password(self.directory, "admin"))

    def test_set_password_raises_explicit_crypto_error_when_pbkdf2_missing(self):
        with mock.patch.object(hashlib, "pbkdf2_hmac", side_effect=AttributeError("no attribute 'pbkdf2_hmac'")):
            with self.assertRaises(wb_settings.PanelPasswordCryptoError) as ctx:
                wb_settings.set_panel_password(self.directory, "newsecret")
            self.assertIn("hashlib.pbkdf2_hmac", str(ctx.exception))

    def test_corrupt_salt_hex_returns_false_safely(self):
        wb_settings.set_panel_password(self.directory, "secret123")
        data = wb_settings.load(self.directory)
        data["panel_password_salt"] = "not_a_valid_hex_string"
        wb_settings.save(self.directory, data)
        self.assertFalse(wb_settings.verify_panel_password(self.directory, "secret123"))


if __name__ == "__main__":
    unittest.main()
