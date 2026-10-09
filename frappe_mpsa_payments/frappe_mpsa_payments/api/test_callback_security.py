from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import get_decrypted_password

from .callback_security import (
    build_callback_urls,
    generate_callback_token,
    verify_callback_token,
)
from ..doctype.mpesa_settings.test_mpesa_settings import create_mpesa_settings


class TestCallbackSecurity(FrappeTestCase):
    def setUp(self) -> None:
        self.settings = create_mpesa_settings("_Callback Security Test")
        self.settings_name = self.settings.name
        self.settings.reload()

    def test_token_is_created_when_callback_urls_are_built(self) -> None:
        settings = create_mpesa_settings("_Callback First URL Test")
        self.assertFalse(
            get_decrypted_password(
                "Mpesa Settings", settings.name, "callback_token", raise_exception=False
            )
        )
        urls = build_callback_urls(settings.name)
        token = get_decrypted_password(
            "Mpesa Settings", settings.name, "callback_token", raise_exception=False
        )
        self.assertTrue(token)
        self.assertEqual(len(token), 43)
        self.assertEqual(parse_qs(urlparse(urls["validation"]).query)["token"][0], token)

    def test_missing_token_is_rejected(self) -> None:
        with self.assertRaises(frappe.PermissionError):
            verify_callback_token(None, self.settings_name)

    def test_wrong_token_is_rejected(self) -> None:
        with self.assertRaises(frappe.PermissionError):
            verify_callback_token("wrong-secret", self.settings_name)

    def test_valid_token_is_accepted(self) -> None:
        self.token = generate_callback_token(self.settings_name)
        self.assertTrue(verify_callback_token(self.token, self.settings_name))

    def test_rejected_callback_error_log_does_not_include_token(self) -> None:
        rejected_token = "must-never-appear-in-error-log"
        with self.assertRaises(frappe.PermissionError):
            verify_callback_token(rejected_token, self.settings_name)
        error = frappe.get_all(
            "Error Log",
            filters={"method": "M-Pesa callback authentication failed"},
            fields=["error"],
            order_by="creation desc",
            limit=1,
        )
        self.assertTrue(error)
        self.assertNotIn(rejected_token, error[0].error)

    def test_generated_token_and_callback_urls_remain_stable(self) -> None:
        before = build_callback_urls(self.settings_name)
        old_token = parse_qs(urlparse(before["validation"]).query)["token"][0]
        self.assertEqual(generate_callback_token(self.settings_name), old_token)
        after = build_callback_urls(self.settings_name)
        new_token = parse_qs(urlparse(after["validation"]).query)["token"][0]
        self.assertEqual(old_token, new_token)
        self.assertEqual(before["validation"], after["validation"])
        self.assertEqual(
            parse_qs(urlparse(after["confirmation"]).query)["token"][0], new_token
        )
