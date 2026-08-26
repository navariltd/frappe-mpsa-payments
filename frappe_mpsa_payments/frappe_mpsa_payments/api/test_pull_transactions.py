"""Pull transaction regression tests.

Deliberately self-contained: these mock at the frappe boundary rather than
building Mpesa Settings / Company / POS Profile fixtures, so they run on any
site. test_mpesa_settings.py needs a specific company fixture and cannot run
on most benches, which is how the 1001-reported-as-success bug survived.
"""

import unittest
from unittest.mock import patch

import frappe

from frappe_mpsa_payments.frappe_mpsa_payments.api import m_pesa_api as api
from frappe_mpsa_payments.frappe_mpsa_payments.api import (
    mpesa_response_handler as handler,
)

SETTINGS_NAME = "_Pull Test"


class _StubSettings:
    name = SETTINGS_NAME
    sandbox = 0
    business_shortcode = "898048"
    till_number = None


class TestPullTransactionResponses(unittest.TestCase):
    """Safaricom signals failure in the body while returning HTTP 200."""

    def _run(self, response, batch=False):
        published = []
        logged = []

        frappe.flags[handler.BULK_PULL_BATCH_FLAG] = batch
        try:
            with (
                patch.object(handler.frappe, "get_doc", return_value=_StubSettings()),
                patch.object(handler, "record_pull_outcome"),
                patch.object(
                    handler, "publish_pull_result", side_effect=published.append
                ),
                patch.object(
                    handler.frappe,
                    "log_error",
                    side_effect=lambda *a, **k: logged.append(k.get("title")),
                ),
            ):
                handler.pull_transaction_on_success(
                    response=response,
                    document_name=SETTINGS_NAME,
                    settings_name=SETTINGS_NAME,
                    integration_request=None,
                    payload={"ShortCode": "898048"},
                )
        finally:
            frappe.flags[handler.BULK_PULL_BATCH_FLAG] = False

        return published[0] if published else {}, logged

    def test_1001_is_not_reported_as_a_successful_import(self):
        msg, logged = self._run(
            {
                "ResponseCode": "1001",
                "ResponseMessage": "No records found or Organization Name not available",
            }
        )
        self.assertEqual(msg.get("status"), "warning")
        self.assertEqual(msg.get("response_code"), "1001")
        self.assertEqual(msg.get("count"), 0)
        self.assertIn("1001", msg.get("message", ""))
        self.assertNotIn("record(s) imported", msg.get("message", ""))
        self.assertEqual(len(logged), 1)

    def test_other_error_codes_report_as_error(self):
        msg, _ = self._run(
            {"ResponseCode": "500.003.02", "ResponseMessage": "Internal server error"}
        )
        self.assertEqual(msg.get("status"), "error")
        self.assertEqual(msg.get("count"), 0)

    def test_1000_with_empty_response_is_a_quiet_window(self):
        msg, _ = self._run(
            {"ResponseCode": "1000", "ResponseMessage": "Success", "Response": []}
        )
        self.assertIn("0 record(s) imported", msg.get("message", ""))
        self.assertEqual(msg.get("count"), 0)

    def test_batch_mode_suppresses_the_per_shortcode_log(self):
        """Dozens of shortcodes, pulled hourly, made these logs unreadable."""
        msg, logged = self._run(
            {"ResponseCode": "1001", "ResponseMessage": "No records found"}, batch=True
        )
        self.assertEqual(logged, [])
        self.assertEqual(msg.get("status"), "warning")


class TestPullPagination(unittest.TestCase):
    """Safaricom paginates; we used to import only the first page."""

    def _walk(self, total_pages):
        requested = []

        def fake_request(**kwargs):
            requested.append(int(kwargs["payload"]["OffSetValue"]))
            kwargs["success_callback"](
                response={
                    "ResponseCode": "1000",
                    "Response": [],
                    "TotalPages": total_pages,
                },
                document_name=SETTINGS_NAME,
                settings_name=SETTINGS_NAME,
                integration_request=None,
            )
            return {"ResponseCode": "1000", "TotalPages": total_pages}

        with (
            patch.object(api, "process_request", side_effect=fake_request),
            patch.object(api.frappe, "publish_realtime"),
            patch.object(handler.frappe, "get_doc", return_value=_StubSettings()),
            patch.object(handler, "record_pull_outcome"),
            patch.object(handler.frappe, "log_error"),
        ):
            api.execute_pull_transactions(
                mpesa_settings=SETTINGS_NAME,
                payload={"ShortCode": "898048", "OffSetValue": "0"},
            )
        return requested

    def test_every_page_is_fetched(self):
        self.assertEqual(self._walk(4), [0, 1, 2, 3])

    def test_single_page_stops_immediately(self):
        self.assertEqual(self._walk(1), [0])

    def test_runaway_total_pages_is_capped(self):
        self.assertEqual(len(self._walk(9999)), api.PULL_MAX_PAGES)
