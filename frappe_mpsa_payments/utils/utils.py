import re
from contextlib import contextmanager
from datetime import datetime
from typing import Generator
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import get_request_site_address

from .doctype_names import ACCESS_TOKENS_DOCTYPE, MPESA_EXPRESS_REQUEST_DOCTYPE


def create_payment_gateway(
    gateway: str, settings: str | None = None, controller: str | None = None
) -> None:
    # NOTE: we don't translate Payment Gateway name because it is an internal doctype
    if not frappe.db.exists("Payment Gateway", gateway):
        payment_gateway = frappe.get_doc(
            {
                "doctype": "Payment Gateway",
                "gateway": gateway,
                "gateway_settings": settings,
                "gateway_controller": controller,
            }
        )
        payment_gateway.insert(ignore_permissions=True)


@contextmanager
def erpnext_app_import_guard() -> Generator:
    marketplace_link = (
        '<a href="https://frappecloud.com/marketplace/apps/erpnext">Marketplace</a>'
    )
    github_link = '<a href="https://github.com/frappe/erpnext">GitHub</a>'
    msg = _("erpnext app is not installed. Please install it from {} or {}").format(
        marketplace_link, github_link
    )
    try:
        yield
    except ImportError:
        frappe.throw(msg, title=_("Missing ERPNext App"))


def save_access_token(
    token: str,
    expiry_time: str | datetime,
    fetch_time: str | datetime,
    associated_setting: str,
    doctype: str = ACCESS_TOKENS_DOCTYPE,
) -> bool:
    doc = frappe.new_doc(doctype)

    doc.associated_settings = associated_setting

    doc.access_token = token
    doc.expiry_time = expiry_time
    doc.token_fetch_time = fetch_time

    try:
        doc.save(ignore_permissions=True)
        doc.submit()

        return True

    except Exception:
        # TODO: Not sure what exception is thrown here. Confirm
        frappe.throw("Error Encountered")
        return False


def get_payment_gateway_controller(payment_gateway):
    """Return payment gateway controller"""
    gateway = frappe.get_doc("Payment Gateway", payment_gateway)
    if gateway.gateway_controller is None:
        try:
            return frappe.get_doc(f"{payment_gateway} Settings")
        except Exception:
            frappe.throw(_("{0} Settings not found").format(payment_gateway))
    else:
        try:
            return frappe.get_doc(gateway.gateway_settings, gateway.gateway_controller)
        except Exception:
            frappe.throw(_("{0} Settings not found").format(payment_gateway))


def create_payment_gateway_account(gateway, payment_channel="Email", company=None):
    from erpnext.setup.setup_wizard.operations.install_fixtures import (
        create_bank_account,
    )

    company = company or frappe.get_cached_value(
        "Global Defaults", "Global Defaults", "default_company"
    )
    if not company:
        return

    # NOTE: we translate Payment Gateway account name because that is going to be used by the end user
    bank_account = frappe.db.get_value(
        "Account",
        {"account_name": _(gateway), "company": company},
        ["name", "account_currency"],
        as_dict=1,
    )

    if not bank_account:
        # check for untranslated one
        bank_account = frappe.db.get_value(
            "Account",
            {"account_name": gateway, "company": company},
            ["name", "account_currency"],
            as_dict=1,
        )

    if not bank_account:
        # try creating one
        bank_account = create_bank_account(
            {"company_name": company, "bank_account": _(gateway)}
        )

    if not bank_account:
        frappe.msgprint(
            _("Payment Gateway Account not created, please create one manually.")
        )
        return

    # if payment gateway account exists, return
    if frappe.db.exists(
        "Payment Gateway Account",
        {"payment_gateway": gateway, "currency": bank_account.account_currency},
    ):
        return

    try:
        frappe.get_doc(
            {
                "doctype": "Payment Gateway Account",
                "is_default": 1,
                "payment_gateway": gateway,
                "payment_account": bank_account.name,
                "currency": bank_account.account_currency,
                "payment_channel": payment_channel,
            }
        ).insert(ignore_permissions=True, ignore_if_duplicate=True)

    except frappe.DuplicateEntryError:
        # already exists, due to a reinstall?
        pass


def build_callback_url(endpoint: str) -> str:
    base_url = get_request_site_address(True)
    parsed_url = urlparse(base_url)

    if not (
        parsed_url.hostname == "localhost"
        or parsed_url.hostname.replace(".", "").isdigit()
    ):
        base_url = f"{parsed_url.scheme}://{parsed_url.hostname}"

    return f"{base_url}/api/method/{endpoint}"


def log_and_throw_error(err_msg, context=None):
    frappe.log_error(frappe.get_traceback(), err_msg)
    if context:
        frappe.throw(_(f"{err_msg}: {context}"))


def handle_successful_transaction(request_doc, settings):
    """Handle actions for a successful transaction"""

    if request_doc.reference_doctype == "Payment Entry" and not request_doc.transaction_id:
        return

    if request_doc.get("reference_doctype") and request_doc.get("reference_name"):
        frappe.get_doc(
            request_doc.get("reference_doctype"), request_doc.get("reference_name")
        ).run_method("on_payment_authorized", "Completed")
        set_mpesa_request_reconciled(request_doc)
        frappe.db.commit()

    if "erpnext" in frappe.get_installed_apps():
        if request_doc.reference_doctype == "Payment Request":
            payment_request = frappe.get_doc(
                "Payment Request", request_doc.reference_name
            )
            if payment_request.reference_doctype == "Sales Invoice":
                invoice = frappe.get_doc(
                    "Sales Invoice", payment_request.reference_name
                )
                if invoice.docstatus == 0:
                    try:
                        invoice.submit()
                    except Exception:
                        log_and_throw_error(
                            "Payment Request Submission Error", request_doc.name
                        )
            try:
                payment_request.create_payment_entry()
            except Exception:
                log_and_throw_error("Payment Entry Creation Error", request_doc.name)

            try:
                if (
                    settings.auto_create_sales_invoice
                    and payment_request.reference_doctype == "Sales Order"
                ):
                    from erpnext.selling.doctype.sales_order.sales_order import (
                        make_sales_invoice,
                    )

                    si = make_sales_invoice(
                        payment_request.reference_name, ignore_permissions=True
                    )
                    si.allocate_advances_automatically = True
                    si = si.insert(ignore_permissions=True)
                    si.submit()
            except Exception:
                log_and_throw_error("Sales Invoice Creation Error", request_doc.name)

            frappe.db.set_value(
                "Payment Request", payment_request.name, "status", "Paid"
            )
            set_mpesa_request_reconciled(request_doc)

        elif request_doc.reference_doctype == "Sales Invoice":
            sales_invoice = frappe.get_doc("Sales Invoice", request_doc.reference_name)

            if sales_invoice.get("is_created_using_pos"):
                # Mark payment as received but don't modify the invoice during checkout
                # The POS form will handle payment rows when it submits
                if sales_invoice.docstatus == 0:
                    # Invoice is still in draft (being edited in POS), just mark the request as reconciled
                    set_mpesa_request_reconciled(request_doc)
                else:
                    # Invoice is already submitted, add payment row
                    found_payment = False
                    for pay in sales_invoice.payments:
                        if pay.mode_of_payment == request_doc.payment_gateway:
                            pay.phone_number = request_doc.phone_number
                            pay.amount = float(request_doc.amount)
                            pay.reference_no = request_doc.transaction_id
                            pay.clearance_date = frappe.utils.nowdate()
                            found_payment = True
                            break

                    if not found_payment:
                        sales_invoice.append(
                            "payments",
                            {
                                "mode_of_payment": request_doc.payment_gateway,
                                "phone_number": request_doc.phone_number,
                                "amount": float(request_doc.amount),
                                "reference_no": request_doc.transaction_id,
                                "clearance_date": frappe.utils.nowdate(),
                            },
                        )

                    sales_invoice.save(ignore_permissions=True)
                    set_mpesa_request_reconciled(request_doc)
            else:
                if sales_invoice.docstatus == 0:
                    try:
                        sales_invoice.submit()
                    except Exception:
                        log_and_throw_error(
                            "Sales Invoice Submission Error", request_doc.name
                        )
                try:
                    payment_row = sales_invoice.append("payments", {})
                    payment_row.amount = float(request_doc.amount)
                    payment_row.mode_of_payment = request_doc.payment_gateway
                    payment_row.reference_no = request_doc.transaction_id
                    payment_row.clearance_date = frappe.utils.nowdate()
                except Exception:
                    log_and_throw_error("Payment Creation Error", request_doc.name)

                sales_invoice.save(ignore_permissions=True)
                set_mpesa_request_reconciled(request_doc)

        elif request_doc.reference_doctype == "Sales Invoice Payment":
            try:
                frappe.db.set_value(
                    "Sales Invoice Payment",
                    request_doc.reference_name,
                    {
                        "reference_no": request_doc.transaction_id,
                    },
                )
                set_mpesa_request_reconciled(request_doc)
            except Exception:
                log_and_throw_error(
                    "Sales Invoice Payment Update Error", request_doc.name
                )
        elif request_doc.reference_doctype == "Payment Entry":
            payment_entry = frappe.get_doc("Payment Entry", request_doc.reference_name)
            if payment_entry.docstatus == 0:
                try:
                    payment_entry.reference_no = request_doc.transaction_id
                    payment_entry.custom_mpesa_receipt_number = (
                        request_doc.transaction_id
                    )
                    payment_entry.reference_date = (
                        frappe.utils.getdate(request_doc.transaction_date)
                        if request_doc.transaction_date
                        else frappe.utils.nowdate()
                    )
                    payment_entry.save(ignore_permissions=True)
                    payment_entry.submit()
                    set_mpesa_request_reconciled(request_doc)
                except Exception:
                    log_and_throw_error(
                        "Payment Entry Submission Error", request_doc.name
                    )
    if request_doc.reference_doctype == "Event Booking":
        try:
            frappe.flags.ignore_permissions = True
            event_booking = frappe.get_doc("Event Booking", request_doc.reference_name)
            event_booking.submit()
            event_payment = frappe.get_doc(
                "Event Payment",
                {
                    "reference_docname": event_booking.name,
                    "reference_doctype": "Event Booking",
                },
            )
            frappe.db.set_value(
                "Event Payment",
                event_payment.name,
                {"payment_received": 1, "payment_id": request_doc.transaction_id},
            )
            set_mpesa_request_reconciled(request_doc)
        except Exception:
            log_and_throw_error("Event Booking Submission Error", request_doc.name)

    event_payload = {
        "status": "Success",
        "request_name": request_doc.name,
        "reference_doctype": request_doc.reference_doctype,
        "reference_name": request_doc.reference_name,
        "transaction_id": request_doc.transaction_id,
        "amount": request_doc.amount,
        "phone_number": request_doc.phone_number,
    }

    # Publish to the request owner first so POS clients subscribed as cashier receive updates.
    frappe.publish_realtime(
        event="mpesa_stk_payment_completed",
        message=event_payload,
        user=request_doc.owner,
    )

    # Keep a global event for integrations that are not user-room specific.
    frappe.publish_realtime(
        event="mpesa_stk_payment_completed",
        message=event_payload,
    )


def set_mpesa_request_reconciled(request_doc):
    request_doc.reload()
    request_doc.is_reconciled = 1
    request_doc.save(ignore_permissions=True)


def update_mpesa_request_status(name, status_data):
    """Update the Mpesa Express Request DocType with callback status"""
    frappe.db.set_value(MPESA_EXPRESS_REQUEST_DOCTYPE, name, status_data)
    frappe.publish_realtime(
        event="refresh_form", doctype=MPESA_EXPRESS_REQUEST_DOCTYPE, docname=name
    )


def validate_phone_number(phone_number):
    if not phone_number or len(phone_number) < 9:
        return False

    number = phone_number.strip().replace(" ", "")
    if not re.match(r"^(?:\+254|254|0)(7\d{8}|1\d{8})$", number):
        return False

    return True


@frappe.whitelist()
def get_mode_of_payment_account(mode_of_payment: str, company: str) -> str:
    return frappe.db.get_value(
        "Mode of Payment Account",
        {"parent": mode_of_payment, "company": company},
        "default_account",
    )


@frappe.whitelist()
def convert_amount_to_kes(
    currency: str, amount: float, date: str = None, settings: str = None
) -> float | None:
    """
    Convert the given amount from `currency` to KES.

    Priority:
    1. Check Mpesa Settings → exchange_rates child table
    2. If ERPNext is installed, use get_exchange_rate
    3. Otherwise, return None
    """

    try:
        if settings:
            settings = frappe.get_doc("Mpesa Settings", settings)
    except frappe.DoesNotExistError:
        settings = None

    if settings and settings.exchange_rates:
        for row in settings.exchange_rates:
            if row.currency == currency:
                return float(amount) * float(row.rate)

    if "erpnext" in frappe.get_installed_apps():
        from erpnext.setup.utils import get_exchange_rate

        conversion_rate = get_exchange_rate(
            currency,
            "KES",
            date,
            "for_selling",
        )

        if not conversion_rate:
            frappe.throw("Conversion rate not available to convert amount to KES.")

        return float(amount) * float(conversion_rate)

    return None
