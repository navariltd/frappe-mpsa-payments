from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from .m_pesa_api import (
    confirmation,
    get_mpesa_draft_c2b_payments,
    get_mpesa_mode_of_payment,
    get_token,
    initiate_stk_push,
    submit_mpesa_payment,
    validation,
)


class TestMPesaAPI(FrappeTestCase):
    @patch("requests.get")
    def test_get_token(self, mock_get):
        mock_response = Mock()
        mock_response.json.return_value = {"access_token": "dummy_token"}
        mock_get.return_value = mock_response

        app_key = "dummy_key"
        app_secret = "dummy_secret"
        base_url = "https://example.com"

        token = get_token(app_key, app_secret, base_url)

        self.assertEqual(token, "dummy_token")

    def test_confirmation(self):
        # Test accepted case
        args = {
            "TransactionType": "Payment",
            "TransID": "123456",
            "TransTime": "2024-05-01T12:00:00",
            "TransAmount": 100.0,
            "BusinessShortCode": "123456",
            "BillRefNumber": "BILL001",
            "InvoiceNumber": "INV001",
            "OrgAccountBalance": 500.0,
            "ThirdPartyTransID": "789012",
            "MSISDN": "1234567890",
            "FirstName": "John",
            "MiddleName": "Doe",
            "LastName": "Smith",
        }
        result = confirmation(**args)
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")

        # Test rejected case
        args["TransAmount"] = "invalid_amount"
        result = confirmation(**args)
        self.assertEqual(result["ResultCode"], 1)
        self.assertEqual(result["ResultDesc"], "Rejected")

    def test_validation(self):
        # Test validation always returns accepted
        result = validation()
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")

    def _stk_payload(self, mock_process, **overrides):
        """Fire initiate_stk_push with process_request stubbed, return the payload."""
        # Mirrors initiate_request(): every key is sent, None included.
        args = {
            "payment_gateway": "Mpesa-898102",
            "phone_number": "254727870777",
            "request_amount": 1,
            "doctype": "Mpesa Express Request",
            "document_name": "MEXP-26-08-000004",
            "reference_name": None,
            "account_reference": None,
        }
        args.update(overrides)

        initiate_stk_push(**args)

        self.assertTrue(
            mock_process.called,
            "process_request was never reached - the push was not built",
        )
        return mock_process.call_args.kwargs["payload"]

    @patch("frappe.get_doc")
    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.build_callback_url"
    )
    @patch("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_request")
    def test_stk_push_account_reference_never_null(
        self, mock_process, mock_callback, mock_get_doc
    ):
        """A standalone push - no linked document - must still carry a reference.

        Regression: initiate_request() always passes the reference_name key, with
        None as its value for a push that isn't tied to an invoice. dict.get()
        only falls back to its default when the key is absent, so the default
        never applied and Daraja rejected the null AccountReference with
        400.002.02 "Bad Request - Invalid Remarks".
        """
        mock_callback.return_value = "https://example.com/callback"
        settings = Mock()
        settings.name = "898102"
        settings.business_shortcode = "898102"
        settings.paybill_type = "Pay Bill"
        settings.get_password.return_value = "passkey"
        mock_get_doc.return_value = settings

        # reference_name present but None - the exact shape initiate_request sends.
        payload = self._stk_payload(mock_process, reference_name=None)

        self.assertIsNotNone(payload["AccountReference"])
        self.assertIsNotNone(payload["TransactionDesc"])
        self.assertEqual(payload["AccountReference"], "MEXP-26-08-000004")
        self.assertEqual(payload["TransactionDesc"], "MEXP-26-08-000004")

    @patch("frappe.get_doc")
    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.build_callback_url"
    )
    @patch("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_request")
    def test_stk_push_reference_name_wins_over_document_name(
        self, mock_process, mock_callback, mock_get_doc
    ):
        """A linked document still supplies the reference - unchanged behaviour."""
        mock_callback.return_value = "https://example.com/callback"
        settings = Mock()
        settings.name = "898102"
        settings.business_shortcode = "898102"
        settings.paybill_type = "Pay Bill"
        settings.get_password.return_value = "passkey"
        mock_get_doc.return_value = settings

        payload = self._stk_payload(mock_process, reference_name="SINV-00042")

        self.assertEqual(payload["AccountReference"], "SINV-00042")

    @patch("frappe.get_doc")
    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.build_callback_url"
    )
    @patch("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_request")
    def test_stk_push_prefers_caller_supplied_account_reference(
        self, mock_process, mock_callback, mock_get_doc
    ):
        """The Pay Bill account number wins over anything we could infer.

        A POS supplies this before the sale exists as a document, and it is what
        the customer's statement shows and what returns as BillRefNumber.
        """
        mock_callback.return_value = "https://example.com/callback"
        settings = Mock()
        settings.name = "898102"
        settings.business_shortcode = "898102"
        settings.paybill_type = "Pay Bill"
        settings.get_password.return_value = "passkey"
        mock_get_doc.return_value = settings

        payload = self._stk_payload(
            mock_process,
            account_reference="ACC-2026-001",
            reference_name="SINV-00042",
        )

        self.assertEqual(payload["AccountReference"], "ACC-2026-001")
        self.assertEqual(payload["TransactionDesc"], "ACC-2026-001")

    @patch("frappe.get_doc")
    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.build_callback_url"
    )
    @patch("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_request")
    def test_stk_push_falls_back_when_nothing_identifies_the_push(
        self, mock_process, mock_callback, mock_get_doc
    ):
        """With neither reference nor document name, fall back to a literal."""
        mock_callback.return_value = "https://example.com/callback"
        settings = Mock()
        settings.name = "898102"
        settings.business_shortcode = "898102"
        settings.paybill_type = "Pay Bill"
        settings.get_password.return_value = "passkey"
        mock_get_doc.return_value = settings

        payload = self._stk_payload(
            mock_process, reference_name=None, document_name=None
        )

        self.assertEqual(payload["AccountReference"], "Online Payment")

    @patch("frappe.get_all")
    def test_get_mpesa_mode_of_payment(self, mock_get_all):
        mock_get_all.return_value = [{"mode_of_payment": "Cash"}]

        company = "Test Company"

        modes_of_payment = get_mpesa_mode_of_payment(company)

        self.assertEqual(modes_of_payment, ["Cash"])

    @patch("frappe.get_all")
    def test_get_mpesa_draft_payments(self, mock_get_all):
        mock_get_all.return_value = [{"name": "MP001", "amount": 100.0}]

        company = "Test Company"
        mode_of_payment = "Cash"

        payments = get_mpesa_draft_c2b_payments(
            company, mode_of_payment=mode_of_payment
        )

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]["name"], "MP001")
        self.assertEqual(payments[0]["amount"], 100.0)

    @patch("frappe.get_doc")
    @patch("frappe.get_all")
    def test_submit_mpesa_payment(self, mock_get_all, mock_get_doc):
        mock_get_all.return_value = [{"name": "MP001"}]
        mock_get_doc.return_value = Mock(payment_entry="PE001")

        mpesa_payment = "MP001"
        customer = "Test Customer"

        payment_entry = submit_mpesa_payment(mpesa_payment, customer)

        self.assertEqual(payment_entry, "PE001")
