"""Only credential-shaped 401s park an account (#440 / #435).

A bare 401 from an intermediate reverse proxy is an HTML error page, not
evidence that the token is dead. Parking on it would remove a healthy
credential from rotation until a manual re-login.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts


def make_account():
    return wb_accounts.Account({"uid": "uid-401", "accessToken": "token"})


class AuthFailureTextTests(unittest.TestCase):
    def test_proxy_html_is_not_a_credential_failure(self):
        self.assertFalse(wb_accounts._auth_failure_text(
            "HTTP 401: <html><head><title>401 Authorization Required</title></head></html>"))

    def test_plain_401_and_authorization_are(self):
        for text in ("HTTP 401", "401 Unauthorized", "Authorization failed",
                     "invalid_token", "token expired"):
            with self.subTest(text=text):
                self.assertTrue(wb_accounts._auth_failure_text(text))

    def test_generic_token_wording_is_not_enough(self):
        self.assertFalse(wb_accounts._auth_failure_text("token count exceeded"))
        self.assertFalse(wb_accounts._auth_failure_text("refresh timeout"))


class NoteErrorTests(unittest.TestCase):
    def test_http_401_marks_the_account(self):
        account = make_account()
        account.note_error("HTTP 401")
        self.assertTrue(account.token_expired)
        self.assertFalse(account.ready())

    def test_openresty_401_keeps_the_account_usable(self):
        account = make_account()
        account.note_error("HTTP 401: <html><title>401 Authorization Required</title></html>")
        self.assertFalse(account.token_expired)
        account.cooldown_until = 0
        self.assertTrue(account.ready())

    def test_generic_token_error_does_not_park(self):
        account = make_account()
        account.note_error("token count exceeded")
        self.assertFalse(account.token_expired)
        account.cooldown_until = 0
        self.assertTrue(account.ready())


class NoteSuccessTests(unittest.TestCase):
    """A served request is proof the token is alive, so it lifts the park.

    Regression: the panel's "test" returned a real model reply while the badge
    still read 「凭证失效」 and the account stayed out of rotation, because
    `note_success()` cleared every other penalty but not `token_expired`.
    """

    def test_success_clears_the_credential_park(self):
        account = make_account()
        account.note_error("HTTP 401")
        self.assertTrue(account.token_expired)
        self.assertFalse(account.ready())

        account.note_success()

        self.assertFalse(account.token_expired)
        self.assertTrue(account.ready())
        view = account.public()
        self.assertFalse(view["tokenExpired"])
        self.assertFalse(view["needRelogin"])
        self.assertNotEqual(view["expiresIn"], "expired")

    def test_success_clears_the_park_for_one_model_only_call(self):
        # The panel test and the real dispatch path both pass a model name.
        account = make_account()
        account.note_error("HTTP 401")
        account.note_success(model="deepseek-v4.1-flash")
        self.assertFalse(account.token_expired)
        self.assertTrue(account.ready(model="deepseek-v4.1-flash"))

    def test_success_cannot_resurrect_a_tokenless_account(self):
        # No credential means no credential: clearing the flag must not make an
        # account without an access token dispatchable, and the badge stays red.
        account = wb_accounts.Account({"uid": "uid-no-token"})
        account.token_expired = True
        account.note_success()
        self.assertFalse(account.ready())
        self.assertTrue(account.public()["tokenExpired"])


class AuthChallengeTests(unittest.TestCase):
    """A gateway that names the credential as bad is evidence, HTML body or not.

    APISIX (copilot.tencent.com) answers a dead token with an HTML 401 page and
    puts the reason in `WWW-Authenticate: Bearer ..., error="invalid_token"`.
    Reading only the body made that look exactly like the proxy page of #435, so
    the panel test showed an opaque HTML error and the account stayed in the
    pool while every dispatch bounced.
    """

    @staticmethod
    def error(code=401, challenge=None, url="https://copilot.tencent.com/v2/chat/completions"):
        import email.message
        import urllib.error
        headers = email.message.Message()
        if challenge:
            headers["WWW-Authenticate"] = challenge
        return urllib.error.HTTPError(url, code, "Unauthorized", headers, None)

    def test_invalid_token_outranks_the_html_bail_out(self):
        message = wb_accounts.http_error_message(
            self.error(challenge='Bearer realm="copilot", error="invalid_token", '
                                'error_description="invalid token"'),
            "<html><title>401 Authorization Required</title></html>")
        self.assertTrue(wb_accounts._auth_failure_text(message))

    def test_proxy_page_without_a_challenge_stays_exempt(self):
        message = wb_accounts.http_error_message(
            self.error(), "<html><title>401 Authorization Required</title></html>")
        self.assertEqual(
            message, "HTTP 401: <html><title>401 Authorization Required</title></html>")
        self.assertFalse(wb_accounts._auth_failure_text(message))

        basic = wb_accounts.http_error_message(
            self.error(challenge='Basic realm="staging"'),
            "<html><title>401 Authorization Required</title></html>")
        self.assertFalse(wb_accounts._auth_failure_text(basic))

    def test_challenge_is_bounded_and_whitespace_collapsed(self):
        import email.message
        headers = email.message.Message()
        headers["WWW-Authenticate"] = 'Bearer\n  realm="copilot",\t error="invalid_token"'
        self.assertEqual(wb_accounts.auth_challenge(headers),
                         'Bearer realm="copilot", error="invalid_token"')
        self.assertEqual(wb_accounts.auth_challenge(email.message.Message()), "")
        self.assertEqual(wb_accounts.auth_challenge(None), "")

    def test_truncation_cannot_cut_the_challenge_off(self):
        # Callers cut the label to 80/150 chars before it reaches the panel,
        # so the header has to sit in front of the body.
        message = wb_accounts.http_error_message(
            self.error(challenge='Bearer realm="copilot", error="invalid_token"'),
            "<html>" + "x" * 400, 80)
        self.assertIn("invalid_token", message)
        self.assertLess(len(message), 260)

    def test_panel_probe_message_parks_the_account(self):
        account = make_account()
        account.note_error(wb_accounts.http_error_message(
            self.error(challenge='Bearer realm="copilot", error="invalid_token"'),
            "<html><title>401 Authorization Required</title></html>"), 80)
        self.assertTrue(account.token_expired)
        self.assertFalse(account.ready())
        self.assertIn("invalid_token", account.last_error)


if __name__ == "__main__":
    unittest.main()
