# Copyright (c) 2025, Navari Limited and contributors
# For license information, please see license.txt


import frappe
from frappe.exceptions import DoesNotExistError
from frappe.model.document import Document
from frappe.utils import time

from frappe_mpsa_payments.utils.doctype_names import MPESA_SETTINGS_DOCTYPE

from ....utils.utils import (
    convert_amount_to_kes,
    handle_successful_transaction,
    validate_phone_number,
)
from ...api.m_pesa_api import (
    check_transaction_status,
    initiate_stk_push,
)  # Import the STK push function


class MpesaExpressRequest(Document):
    def set_missing_values(self):
        if not self.request_id:
            self.request_id = f"MPESAEXP{frappe.generate_hash(length=32)}"
            self.route = f"/mpesa/stkpush?id={self.request_id}"
        if self.settings and not self.payment_gateway:
            self.payment_gateway = frappe.db.get_value(
                "Payment Gateway", {"gateway_controller": self.settings}, "name"
            )

        if self.payment_gateway and not self.settings:
            self.settings = frappe.db.get_value(
                "Payment Gateway", {"name": self.payment_gateway}, "gateway_controller"
            )

        if self.currency != "KES":
            settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, self.settings)
            if (
                "erpnext" not in frappe.get_installed_apps()
                and not settings.enable_multi_currency_payments
            ):
                frappe.throw("Mpesa Express Requests must be in KES currency.")

            converted_amount = convert_amount_to_kes(self.currency, self.base_amount)
            if converted_amount is not None:
                self.amount = converted_amount

        else:
            if self.base_amount:
                self.amount = self.base_amount

    def validate(self):
        self.set_missing_values()

        if "erpnext" in frappe.get_installed_apps():
            if self.reference_doctype == "Payment Request":
                self.validate_payment_request_amount()

        if self.phone_number and not validate_phone_number(self.phone_number):
            frappe.throw(
                "Invalid phone number format. Please ensure it is in the correct format, e.g., 254712345678."
            )

    def validate_payment_request_amount(self):
        payment_request = frappe.get_doc("Payment Request", self.reference_name)
        currency = payment_request.currency
        self.amount = payment_request.grand_total
        self.currency = currency
        # company = payment_request.company
        # company_currency = frappe.db.get_value("Company", company, "default_currency")

        # if currency != "KES":
        #     if payment_request.reference_doctype and payment_request.reference_name:
        #         ref_doc = frappe.get_doc(
        #             payment_request.reference_doctype, payment_request.reference_name
        #         )
        #         conversion_rate = getattr(ref_doc, "conversion_rate", None)

        #         if company_currency != "KES":
        #             frappe.throw(
        #                 "STK Push can only be initiated if document or company currency is KES. "
        #                 f"Current currency: {currency}, Company currency: {company_currency}"
        #             )

        #         if not conversion_rate:
        #             frappe.throw(
        #                 "Conversion rate not available to convert amount to KES."
        #             )

        #         self.amount = float(payment_request.grand_total) * float(
        #             conversion_rate
        #         )
        #     else:
        #         frappe.throw("Missing reference document to determine conversion rate.")

    def before_submit(self):
        if not self.amount or self.amount <= 0:
            frappe.throw("Amount must be greater than zero to initiate STK Push.")
        if not self.phone_number:
            frappe.throw("Phone number is required to initiate STK Push.")

    def on_submit(self):
        self.initiate_request()

    @frappe.whitelist()
    def initiate_request(self):
        args = {
            "payment_gateway": self.payment_gateway,
            "phone_number": self.phone_number,
            "request_amount": self.amount,
            "doctype": self.doctype,
            "document_name": self.name,
            "reference_name": self.reference_name,
        }

        try:
            initiate_stk_push(**args)
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "STK Push on Submit Error")
            frappe.throw(f"Failed to initiate STK Push: {str(e)}")

    def on_update_after_submit(self):
        """Neutralize duplicate C2B when transaction_id is finally set after callback."""
        self.validate_duplicate_c2b_records()

    def validate_duplicate_c2b_records(self):
        """Ensure any duplicate C2B is neutralized in favour of this Express Request."""
        if not self.transaction_id:
            return

        c2b_name = frappe.db.exists(
            "Mpesa C2B Payment Register", {"transid": self.transaction_id}
        )

        if not c2b_name:
            return

        frappe.db.savepoint("before_c2b_neutralize")
        try:
            c2b_doc = frappe.get_doc("Mpesa C2B Payment Register", c2b_name)

            if c2b_doc.docstatus == 1:
                c2b_doc.cancel()
            else:
                c2b_doc.delete()

            frappe.log_error(
                message=f"Neutralised duplicate C2B {c2b_doc.name} in favour of Express Request {self.name}",
                title="Mpesa Express vs C2B Duplicate",
            )

        except Exception:
            frappe.db.rollback(save_point="before_c2b_neutralize")
            frappe.log_error(
                frappe.get_traceback(),
                f"Error neutralising duplicate C2B {c2b_name} for Express Request {self.name}",
            )

    @frappe.whitelist()
    def reconcile_payment(self):
        self.reload()
        settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, self.settings)

        if not self.is_reconciled:
            handle_successful_transaction(self, settings)


@frappe.whitelist(allow_guest=True)
def create_new_request(
    phone_number,
    payment_gateway,
    reference_type,
    reference_id,
    amount,
    currency="KES",
    redirect=None,
    title="",
    description="",
):
    phone_number = clean_digits(phone_number)
    if (
        not phone_number
        or not payment_gateway
        or not reference_type
        or not reference_id
        or not amount
    ):
        frappe.throw("Missing fields")

    doc = frappe.get_doc(
        {
            "doctype": "Mpesa Express Request",
            "phone_number": phone_number,
            "payment_gateway": payment_gateway,
            "reference_doctype": reference_type,
            "reference_name": reference_id,
            "base_amount": float(amount),
            "currency": currency,
            "status": "In Progress",
            "transaction_title": title,
            "transaction_description": description,
            "redirect": redirect,
        }
    )
    doc.insert(ignore_permissions=True)
    doc.submit()
    frappe.db.commit()

    return doc.name


def clean_digits(v):
    if not v:
        return ""
    return "".join(filter(str.isdigit, str(v)))


@frappe.whitelist(allow_guest=True)
def get_request_status(name):
    frappe.flags.ignore_permissions = True

    try:
        doc = frappe.get_doc("Mpesa Express Request", name)
    except DoesNotExistError:
        frappe.throw(f"Mpesa Express Request {name} not found.")

    max_retries = 10
    wait_time_seconds = 3
    retries = 0

    while doc.status == "Pending" and retries < max_retries:
        check_transaction_status(doc.name)
        time.sleep(wait_time_seconds)

        doc.reload()

        retries += 1

    return {
        "status": doc.status,
        "amount": doc.amount,
        "phone_number": doc.phone_number,
    }


@frappe.whitelist(allow_guest=True)
def get_stkpush_defaults():
    gateways = frappe.get_all(
        "Payment Gateway",
        filters={"gateway_settings": "Mpesa Settings"},
        fields=["name", "gateway_controller"],
        ignore_permissions=True,
    )

    reference_map = {}
    for g in gateways:
        settings_name = g.gateway_controller
        if settings_name:
            settings = frappe.get_doc(
                "Mpesa Settings", settings_name, ignore_permissions=True
            )
            reference_map[g.name] = [
                d.target_doctype for d in settings.reconciliation_order
            ]
        else:
            reference_map[g.name] = []

    return {"gateways": gateways, "reference_map": reference_map}


@frappe.whitelist(allow_guest=True)
def retry_stkpush(name):
    doc = frappe.get_doc("Mpesa Express Request", name)
    if doc.status == "Completed":
        frappe.throw("Cannot retry a completed STK Push request.")

    doc.status = "In Progress"
    doc.save(ignore_permissions=True)
    doc.initiate_request()
