import frappe
from frappe.tests.utils import FrappeTestCase
from frappe_mpsa_payments.frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry import (
    get_outstanding_invoices,
    get_unallocated_payments,
    process_pos_payment,
    get_available_pos_profiles,
    set_paid_amount_and_received_amount,
    create_payment_entry,
    get_mode_of_payment,
    create_and_reconcile_payment_reconciliation,
)

class TestPaymentFunctions(FrappeTestCase):
    def setUp(self):
        self.company = "Test Company Maniac"
        self.currency = "KES"
        self.customer = "Test Customer"
        self.pos_profile_name = "Test POS Profile"

        # Ensure required test records exist
        if not frappe.db.exists("Customer", self.customer):
            frappe.get_doc({
                "doctype": "Customer",
                "customer_name": self.customer
            }).insert()

        if not frappe.db.exists("Company", self.company):
            frappe.get_doc({
                "doctype": "Company",
                "company_name": self.company
            }).insert()

        if not frappe.db.exists("POS Profile", self.pos_profile_name):
            frappe.get_doc({
                "doctype": "POS Profile",
                "name": self.pos_profile_name,
                "company": self.company,
                "currency": self.currency,
                "payments": [
                    {"mode_of_payment": "Cash", "default": 1},
                    {"mode_of_payment": "Card", "default": 0}
                ]
            }).insert()

        if not frappe.db.exists("Item", "Test Item"):
            frappe.get_doc({
                "doctype": "Item",
                "item_code": "Test Item",
                "item_name": "Test Item",
                "stock_uom": "Nos"
            }).insert()

    def test_get_outstanding_invoices(self):
        invoices = get_outstanding_invoices(
            self.company, self.currency, self.customer, self.pos_profile_name
        )
        self.assertIsInstance(invoices, list)

    def test_get_unallocated_payments(self):
        unallocated_payments = get_unallocated_payments(
            self.customer, self.company, self.currency, "Cash"
        )
        self.assertIsInstance(unallocated_payments, list)

    def test_process_pos_payment(self):
        payload = {
            "company": self.company,
            "currency": self.currency,
            "customer": self.customer,
            "pos_opening_shift_name": "Test Opening Shift",
            "pos_profile": {
                "name": self.pos_profile_name,
                "custom_allow_make_new_payments": 1,
                "custom_allow_make_new_invoices": 1,
                "custom_use_pos_payments": 1
            },
            "pos_profile_name": self.pos_profile_name,
            "selected_invoices": [],
            "selected_payments": [],
            "selected_mpesa_payments": [],
            "total_selected_invoices": 0,
            "total_selected_payments": 0,
            "total_selected_mpesa_payments": 0,
            "payment_methods": [
                {"mode_of_payment": "Cash", "amount": 50.00}
            ],
            "total_payment_methods": 50.00,
        }

        result = process_pos_payment(payload)
        self.assertIsInstance(result, dict)

    def test_get_available_pos_profiles(self):
        pos_profiles = get_available_pos_profiles(self.company, self.currency)
        self.assertIsInstance(pos_profiles, list)

    def test_set_paid_amount_and_received_amount(self):
        party_account_currency = self.currency
        bank = {
            "account_currency": self.currency,
            "bank_currency": self.currency,
            "conversion_rate": 1.0
        }
        outstanding_amount = 100.00
        payment_type = "Receive"
        bank_amount = None
        conversion_rate = 1.0

        paid_amount, received_amount = set_paid_amount_and_received_amount(
            party_account_currency, bank, outstanding_amount,
            payment_type, bank_amount, conversion_rate
        )

        self.assertEqual(paid_amount, 100.00)
        self.assertEqual(received_amount, 100.00)

    def test_create_payment_entry(self):
        pe = create_payment_entry(
            company=self.company,
            customer=self.customer,
            amount=100,
            currency=self.currency,
            mode_of_payment="Cash",
            submit=0
        )

        self.assertEqual(pe.doctype, "Payment Entry")
        self.assertEqual(pe.party, self.customer)
        self.assertEqual(pe.company, self.company)
        self.assertEqual(pe.paid_amount, 100)

    def test_get_mode_of_payment(self):
        mop = get_mode_of_payment(self.pos_profile_name)
        self.assertEqual(mop, "Cash")

    def test_create_and_reconcile_payment_reconciliation(self):
        invoice = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": self.customer,
            "company": self.company,
            "currency": self.currency,
            "items": [{"item_code": "Test Item", "qty": 1, "rate": 100}]
        }).insert()
        invoice.submit()

        pe = create_payment_entry(
            company=self.company,
            customer=self.customer,
            amount=100,
            currency=self.currency,
            mode_of_payment="Cash",
            submit=1
        )

        create_and_reconcile_payment_reconciliation(
            [invoice.name], self.customer, self.company, [pe.name]
        )

        exists = frappe.db.exists("Payment Reconciliation", {
            "party": self.customer,
            "company": self.company
        })
        self.assertTrue(exists)


   
    
    
     
    
     
        
