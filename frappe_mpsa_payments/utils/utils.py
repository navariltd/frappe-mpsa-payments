from contextlib import contextmanager
from datetime import datetime
from typing import Generator

import frappe
from frappe import _

from .doctype_names import ACCESS_TOKENS_DOCTYPE


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
