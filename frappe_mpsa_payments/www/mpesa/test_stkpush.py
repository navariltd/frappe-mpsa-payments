# Copyright (c) 2025, Navari Limited and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from .stkpush import check_payment_status, initiate_stk_push, retry_stkpush


class TestStkPushCheckout(FrappeTestCase):
    """The checkout endpoints are open to anyone, so the token is the only guard.

    They must address the request by its request_id - 32 hex characters from
    frappe.generate_hash - and never by document name. Names are sequential, so
    accepting one would let anyone walk MEXP-26-08-0000NN and drive other
    people's payments.
    """

    TOKEN = "MPESAEXP07958e43e3655421c98302d49db1f6f2"

    def _doc(self):
        doc = MagicMock()
        doc.status = "In Progress"
        doc.docstatus = 0
        doc.response_description = None
        doc.redirect_to = None
        doc.reference_doctype = None
        doc.is_reconciled = 0
        return doc

    def _filters_used(self, mock_get_doc):
        self.assertTrue(mock_get_doc.called, "no lookup was performed")
        return mock_get_doc.call_args.args[1]

    @patch("frappe.get_doc")
    def test_check_payment_status_looks_up_by_token(self, mock_get_doc):
        mock_get_doc.return_value = self._doc()

        check_payment_status(self.TOKEN)

        self.assertEqual(self._filters_used(mock_get_doc), {"request_id": self.TOKEN})

    @patch("frappe.get_doc")
    def test_initiate_looks_up_by_token(self, mock_get_doc):
        mock_get_doc.return_value = self._doc()

        initiate_stk_push(self.TOKEN, "254727870777")

        self.assertEqual(self._filters_used(mock_get_doc), {"request_id": self.TOKEN})

    @patch("frappe.get_doc")
    def test_retry_looks_up_by_token(self, mock_get_doc):
        mock_get_doc.return_value = self._doc()

        retry_stkpush(self.TOKEN, "254727870777")

        self.assertEqual(self._filters_used(mock_get_doc), {"request_id": self.TOKEN})

    @patch("frappe.get_doc")
    def test_a_document_name_is_never_used_as_a_lookup_key(self, mock_get_doc):
        """Passing a document name must not resolve that document."""
        mock_get_doc.return_value = self._doc()

        check_payment_status("MEXP-26-08-000004")

        filters = self._filters_used(mock_get_doc)
        self.assertEqual(filters, {"request_id": "MEXP-26-08-000004"})
        # The value lands in the token field, so a real lookup finds nothing.
        self.assertNotIn("name", filters)
