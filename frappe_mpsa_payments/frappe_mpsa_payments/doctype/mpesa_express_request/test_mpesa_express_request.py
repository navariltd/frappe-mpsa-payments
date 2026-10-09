# Copyright (c) 2025, Navari Limited and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

PUSH = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype"
    ".mpesa_express_request.mpesa_express_request.initiate_stk_push"
)


class TestMpesaExpressRequest(FrappeTestCase):
    def _draft(self, **fields):
        doc = frappe.new_doc("Mpesa Express Request")
        doc.name = "MEXP-TEST-0001"
        doc.payment_gateway = "Mpesa-898102"
        doc.phone_number = "254727870777"
        doc.amount = 1
        doc.update(fields)
        return doc

    @patch(PUSH)
    def test_initiate_request_forwards_account_reference(self, mock_push):
        """The Pay Bill account number has to reach the push, not just the doc.

        Regression: a POS could set account_reference and see it stored, while
        initiate_request() never forwarded it, so Safaricom was sent the request
        name instead and the account number never reached the statement.
        """
        self._draft(account_reference="ACC-2026-001").initiate_request()

        self.assertEqual(
            mock_push.call_args.kwargs.get("account_reference"), "ACC-2026-001"
        )

    @patch(PUSH)
    def test_initiate_request_forwards_all_reference_candidates(self, mock_push):
        """Whatever the push falls back to, initiate_request must supply it."""
        doc = self._draft(
            account_reference=None,
            reference_doctype="Sales Invoice",
            reference_name="SINV-00042",
        )
        doc.initiate_request()

        kwargs = mock_push.call_args.kwargs
        self.assertEqual(kwargs.get("reference_name"), "SINV-00042")
        self.assertEqual(kwargs.get("document_name"), "MEXP-TEST-0001")
