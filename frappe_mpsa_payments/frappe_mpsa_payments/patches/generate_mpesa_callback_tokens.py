"""Provision callback secrets for settings that predate token authentication."""

import frappe
from frappe.utils.password import get_decrypted_password

from frappe_mpsa_payments.frappe_mpsa_payments.api.callback_security import (
    generate_callback_token,
)


def execute() -> None:
    settings_names = frappe.get_all("Mpesa Settings", pluck="name")
    generated = 0
    for settings_name in settings_names:
        if not get_decrypted_password(
            "Mpesa Settings", settings_name, "callback_token", raise_exception=False
        ):
            generate_callback_token(settings_name)
            generated += 1

    if generated:
        frappe.logger("frappe_mpsa_payments").warning(
            "Generated callback tokens for %s M-Pesa settings record(s). "
            "Re-register C2B callback URLs with Safaricom before relying on callbacks.",
            generated,
        )
