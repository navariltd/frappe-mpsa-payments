"""Shared-secret authentication for unsigned Safaricom callbacks."""

from __future__ import annotations

import hmac
import hashlib
import secrets
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import get_url, now_datetime
from frappe.utils.password import get_decrypted_password

TOKEN_FIELD = "callback_token"
CALLBACK_PATH = "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api"


def _decrypted(doc, fieldname: str) -> str | None:
    return get_decrypted_password(
        "Mpesa Settings", doc.name, fieldname, raise_exception=False
    )


def generate_callback_token(settings_name: str) -> str:
    """Create a settings token once, returning the existing secret later."""
    doc = frappe.get_doc("Mpesa Settings", settings_name)
    existing = _decrypted(doc, TOKEN_FIELD)
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    doc.set_password(TOKEN_FIELD, token)
    doc.db_set("callback_token_updated_on", now_datetime(), update_modified=False)
    return token


def verify_callback_token(token: str | None, settings_name: str | None) -> bool:
    """Authenticate against the named settings record without exposing failure details."""
    valid = False
    resolved_name = _resolve_settings_name(settings_name)
    if resolved_name:
        doc = frappe.get_doc("Mpesa Settings", resolved_name)
        current = _decrypted(doc, TOKEN_FIELD)
        if isinstance(token, str) and token and current:
            valid = hmac.compare_digest(token, current)
    if not valid:
        ip = getattr(getattr(frappe, "local", None), "request_ip", None)
        if not ip:
            ip = getattr(getattr(frappe, "request", None), "remote_addr", "unknown")
        frappe.log_error(
            title="M-Pesa callback authentication failed",
            message=f"Rejected callback from source IP: {ip or 'unknown'}",
        )
        frappe.throw(_("Forbidden"), frappe.PermissionError)
    return True


def get_callback_settings(settings_selector: str | None):
    """Load the settings record selected by an already-verified callback token.

    Call this only after ``verify_callback_token`` so unauthenticated callers
    cannot use selector lookups to probe settings records.
    """
    resolved_name = _resolve_settings_name(settings_selector)
    if not resolved_name:
        frappe.throw(_("Forbidden"), frappe.PermissionError)
    return frappe.get_doc("Mpesa Settings", resolved_name)


def _settings_id(settings_name: str) -> str:
    return hashlib.sha256(settings_name.encode("utf-8")).hexdigest()[:24]


def _resolve_settings_name(settings_id: str | None) -> str | None:
    """Resolve the opaque selector without exposing record names in registered URLs."""
    if not isinstance(settings_id, str) or not settings_id:
        return None
    for name in frappe.get_all("Mpesa Settings", pluck="name"):
        if hmac.compare_digest(settings_id, _settings_id(name)):
            return name
    # Direct names keep the public helper convenient for trusted internal callers.
    if frappe.db.exists("Mpesa Settings", settings_id):
        return settings_id
    return None


def build_callback_urls(settings_name: str) -> dict[str, str]:
    """Build registered C2B and Transaction Status callback URLs."""
    doc = frappe.get_doc("Mpesa Settings", settings_name)
    token = _decrypted(doc, TOKEN_FIELD)
    if not token:
        token = generate_callback_token(settings_name)
    base_url = get_url().rstrip("/") + "/api/method/" + CALLBACK_PATH
    query = urlencode({"s": _settings_id(settings_name), "token": token})
    return {
        "balance_result": f"{base_url}.balance_query_callback?{query}",
        "validation": f"{base_url}.validation?{query}",
        "confirmation": f"{base_url}.confirmation?{query}",
        "result": f"{base_url}.handle_transaction_status_result?{query}",
        "queue_timeout": f"{base_url}.handle_queue_timeout?{query}",
    }
