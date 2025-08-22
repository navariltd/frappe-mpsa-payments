import hashlib
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


def handle_successful_transaction(
    request_doc, metadata_dict, settings, checkout_request_id
):
    """Handle actions for a successful transaction"""
    if request_doc.reference_doctype == "Payment Request":
        payment_request = frappe.get_doc("Payment Request", request_doc.reference_name)
        try:
            payment_request.create_payment_entry()
        except Exception:
            log_and_throw_error("Payment Entry Creation Error", checkout_request_id)

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
            log_and_throw_error("Sales Invoice Creation Error", checkout_request_id)

        frappe.db.set_value("Payment Request", payment_request.name, "status", "Paid")

    elif request_doc.reference_doctype == "Sales Invoice":
        sales_invoice = frappe.get_doc("Sales Invoice", request_doc.reference_name)
        try:
            payment_row = sales_invoice.append("payments", {})
            payment_row.amount = float(metadata_dict.get("Amount", 0))
            payment_row.mode_of_payment = request_doc.payment_gateway
            payment_row.reference_no = metadata_dict.get("MpesaReceiptNumber")
            payment_row.clearance_date = frappe.utils.nowdate()
            sales_invoice.save(ignore_permissions=True)
        except Exception:
            log_and_throw_error("Payment Creation Error", checkout_request_id)

    elif request_doc.reference_doctype == "Sales Invoice Payment":
        try:
            frappe.db.set_value(
                "Sales Invoice Payment",
                request_doc.reference_name,
                {
                    "reference_no": metadata_dict.get("MpesaReceiptNumber"),
                },
            )
        except Exception:
            log_and_throw_error(
                "Sales Invoice Payment Update Error", checkout_request_id
            )


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


frappe.utils.logger.set_log_level("INFO")
logger = frappe.logger("mpesa_msisdn_hashing", allow_site=True, file_count=5)


def hash_msisdn(msisdn: str) -> str:
    return hashlib.sha256(msisdn.encode("utf-8")).hexdigest()


def normalize_msisdn(msisdn: str) -> str:
    msisdn = msisdn.strip()
    if msisdn.startswith("0"):
        return "254" + msisdn[1:]
    elif msisdn.startswith("+254"):
        return msisdn[1:]
    elif msisdn.startswith("254"):
        return msisdn
    else:
        # fallback: remove non-digits, assume Kenyan number
        digits = re.sub(r"\D", "", msisdn)
        if digits.startswith("7") or digits.startswith("1"):
            return "254" + digits
        return digits


def update_phone_hashes_in_contacts():
    phone_entries = frappe.get_all(
        "Contact Phone", fields=["name", "phone", "mpesa_msisdn_hash"]
    )

    for entry in phone_entries:
        if not entry.phone:
            continue

        if entry.mpesa_msisdn_hash:
            continue  # already set

        normalized = normalize_msisdn(entry.phone)
        msisdn_hash = hash_msisdn(normalized)

        frappe.db.set_value(
            "Contact Phone",
            entry.name,
            "mpesa_msisdn_hash",
            msisdn_hash,
            update_modified=False,
        )
        logger.info(f"[HASHED] {entry.name} -> {normalized} -> {msisdn_hash}")

    frappe.db.commit()

    logger.info("Finished updating mpesa_msisdn_hash in Contact Phone.")


def auto_hash_contact_phone(doc, method):
    for phone in doc.phone_nos:
        if phone.phone and not phone.mpesa_msisdn_hash:
            normalized = normalize_msisdn(phone.phone)
            msisdn_hash = hash_msisdn(normalized)
            frappe.db.set_value(
                "Contact Phone",
                phone.name,
                "mpesa_msisdn_hash",
                msisdn_hash,
                update_modified=False,
            )
            frappe.db.commit()
            frappe.publish_realtime(
                event="refresh_form", doctype=doc.doctype, docname=doc.name
            )


def resolve_msisdn_from_hash(msisdn_hash: str) -> str | None:
    result = frappe.db.get_value(
        "Contact Phone", {"mpesa_msisdn_hash": msisdn_hash}, "phone"
    )
    if result:
        return normalize_msisdn(result)
    return None
