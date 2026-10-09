from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from .callback_security import build_callback_urls, generate_callback_token
from .m_pesa_api import (
    confirmation,
    get_mpesa_draft_c2b_payments,
    get_mpesa_mode_of_payment,
    get_token,
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
        from ..doctype.mpesa_settings.test_mpesa_settings import create_mpesa_settings

        settings = create_mpesa_settings("_Callback API Test")
        generate_callback_token(settings.name)
        callback_url = build_callback_urls(settings.name)["confirmation"]
        from urllib.parse import parse_qs, urlparse

        credentials = {key: value[0] for key, value in parse_qs(urlparse(callback_url).query).items()}
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
            **credentials,
        }
        result = confirmation(**args)
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")

    def test_validation(self):
        from ..doctype.mpesa_settings.test_mpesa_settings import create_mpesa_settings

        settings = create_mpesa_settings("_Callback Validation Test")
        generate_callback_token(settings.name)
        callback_url = build_callback_urls(settings.name)["validation"]
        from urllib.parse import parse_qs, urlparse

        credentials = {key: value[0] for key, value in parse_qs(urlparse(callback_url).query).items()}
        result = validation(**credentials)
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")

    @patch("frappe.get_all")
    def test_get_mpesa_mode_of_payment(self, mock_get_all):
        mock_mode1 = Mock()
        mock_mode1.mode_of_payment = "Cash"

        mock_mode2 = Mock()
        mock_mode2.mode_of_payment = "M-Pesa"

        mock_get_all.return_value = [mock_mode1, mock_mode2]

        company = "Test Company"

        modes_of_payment = get_mpesa_mode_of_payment(company)

        self.assertEqual(modes_of_payment, ["Cash", "M-Pesa"])

    @patch("frappe.get_all")
    def test_get_mpesa_draft_c2b_payments(self, mock_get_all):
        mock_get_all.return_value = [{"name": "MP001", "amount": 100.0}]

        company = "Test Company"
        mode_of_payment = "Cash"

        payments = get_mpesa_draft_c2b_payments(company, mode_of_payment)

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]["name"], "MP001")
        self.assertEqual(payments[0]["amount"], 100.0)

    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.get_mode_of_payment"
    )
    @patch("frappe.get_doc")
    def test_submit_mpesa_payment(self, mock_get_doc, mock_get_mode_of_payment):
        mock_mpesa_doc = Mock()
        mock_mpesa_doc.customer = None
        mock_mpesa_doc.mode_of_payment = None
        mock_mpesa_doc.submit_payment = None
        mock_mpesa_doc.payment_entry = "PE001"
        mock_mpesa_doc.businessshortcode = "123456"

        mock_payment_entry = Mock()
        mock_payment_entry.name = "PE001"

        def get_doc_side_effect(doctype, name=None):
            if doctype == "Mpesa C2B Payment Register":
                return mock_mpesa_doc
            elif doctype == "Payment Entry":
                return mock_payment_entry
            return Mock()

        mock_get_doc.side_effect = get_doc_side_effect
        mock_get_mode_of_payment.return_value = "M-Pesa"

        mpesa_payment = "MP001"
        customer = "Test Customer"

        payment_entry = submit_mpesa_payment(mpesa_payment, customer)

        self.assertEqual(mock_mpesa_doc.customer, customer)
        mock_mpesa_doc.save.assert_called_once()
        mock_mpesa_doc.submit.assert_called_once()

        self.assertEqual(payment_entry, mock_payment_entry)
