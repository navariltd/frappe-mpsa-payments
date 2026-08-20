import frappe
from frappe import _


@frappe.whitelist()
def initiate_invoice_stk_push(
    invoice: str = None,
    phone_number: str = None,
    amount: float = None,
    currency: str = "KES",
    mode_of_payment: str = None,
    company: str = None,
    type: str = "Sales Invoice",
) -> dict:
    try:
        if not (
            invoice and phone_number and amount and mode_of_payment and company and type
        ):
            frappe.throw(
                _(
                    "All fields (invoice, phone_number, amount, mode_of_payment, company, type) are required."
                )
            )

        if not isinstance(amount, (int, float)) or float(amount) <= 0:
            frappe.throw(_("Amount must be greater than zero."))

        payment_gateway = get_payment_gateway_from_mop(mode_of_payment, company)

        express_request = frappe.get_doc(
            {
                "doctype": "Mpesa Express Request",
                "reference_name": invoice,
                "reference_doctype": type,
                "phone_number": phone_number,
                "base_amount": float(amount),
                "currency": currency,
                "payment_gateway": payment_gateway,
                "settings": payment_gateway[6:],
            }
        )
        express_request.insert(ignore_permissions=True)
        express_request.submit()

        return {
            "status": "success",
            "message": f"STK Push for {invoice} initiated to {phone_number} via {payment_gateway}.",
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "initiate_invoice_stk_push Error")
        return {
            "status": "error",
            "message": _(
                "Failed to initiate STK Push. Please check the logs for more details."
            ),
        }


@frappe.whitelist()
def validate_stk_push_eligibility(docname, doctype):
    """Validate if STK push can be initiated for this document"""
    try:
        doc = frappe.get_doc(doctype, docname)

        if doc.docstatus != 0 and not doc.is_pos:
            return {
                "eligible": False,
                "message": _("Only allowed for POS invoices or drafts"),
            }

        if doc.outstanding_amount <= 0:
            return {"eligible": False, "message": _("No outstanding amount")}

        # Check currency compatibility
        company_currency = frappe.get_value("Company", doc.company, "default_currency")
        party_account_currency = frappe.get_value(
            "Account", doc.debit_to, "account_currency"
        )

        if not (
            doc.currency == "KES"
            or party_account_currency == "KES"
            or company_currency == "KES"
        ):
            return {"eligible": False, "message": _("Only KES payments supported")}

        # Check for Mpesa payment modes
        mpesa_modes = frappe.get_all(
            "Mode of Payment", filters={"name": ["like", "Mpesa%"]}
        )
        if not mpesa_modes:
            return {
                "eligible": False,
                "message": _("No Mpesa payment modes configured"),
            }

        # Calculate amount in KES
        amount = doc.outstanding_amount
        if party_account_currency != "KES":
            if not doc.conversion_rate:
                return {
                    "eligible": False,
                    "message": _("Conversion rate not available"),
                }
            amount = amount * doc.conversion_rate

        return {"eligible": True, "amount": amount}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "validate_stk_push_eligibility Error")
        return {
            "eligible": False,
            "message": _(
                "Error validating STK Push eligibility. Please check the logs."
            ),
        }


@frappe.whitelist()
def initiate_row_stk_push(
    name: str = None,
    phone_number: str = None,
    amount: float = None,
    currency: str = "KES",
    mode_of_payment: str = None,
    company: str = None,
) -> dict:
    try:
        if not (name and phone_number and amount and mode_of_payment and company):
            frappe.throw(
                _(
                    "All fields (name, phone_number, amount, mode_of_payment, company) are required."
                )
            )

        if not isinstance(amount, (int, float)) or float(amount) <= 0:
            frappe.throw(_("Amount must be greater than zero."))

        payment = frappe.get_doc("Sales Invoice Payment", name)
        if payment.type != "Phone" or payment.reference_no:
            frappe.throw(
                _("STK Push can only be initiated for Phone payments without reference")
            )

        payment_gateway = get_payment_gateway_from_mop(mode_of_payment, company)

        express_request = frappe.get_doc(
            {
                "doctype": "Mpesa Express Request",
                "reference_name": name,
                "reference_doctype": "Sales Invoice Payment",
                "phone_number": phone_number,
                "base_amount": float(amount),
                "currency": currency,
                "payment_gateway": payment_gateway,
                "settings": payment_gateway[6:],
            }
        )
        express_request.insert(ignore_permissions=True)
        express_request.submit()

        return {
            "status": "success",
            "message": f"STK Push for payment {name} initiated to {phone_number} via {payment_gateway}.",
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "initiate_row_stk_push Error")
        return {
            "status": "error",
            "message": _(
                "Failed to initiate STK Push for payment. Please check the logs for more details."
            ),
        }


@frappe.whitelist()
def get_payment_gateway_from_mop(mode_of_payment: str, company: str) -> str:
    payment_gateway = None
    try:
        if not frappe.db.exists("Mode of Payment", mode_of_payment):
            return None
        mop_doc = frappe.get_doc("Mode of Payment", mode_of_payment)
        account_entry = next(
            (acc for acc in mop_doc.accounts if acc.company == company), None
        )
        if account_entry:
            payment_account = account_entry.default_account
            if frappe.db.exists(
                "Payment Gateway Account", {"payment_account": payment_account}
            ):
                try:
                    pg_account = frappe.get_doc(
                        "Payment Gateway Account", {"payment_account": payment_account}
                    )
                    if pg_account and pg_account.payment_gateway:
                        payment_gateway = pg_account.payment_gateway
                except Exception:
                    pass
            else:
                default_pg_account = frappe.get_value(
                    "Payment Gateway Account", {"is_default": 1}, "payment_gateway"
                )
                if default_pg_account:
                    payment_gateway = default_pg_account
    except Exception:
        pass

    return payment_gateway


@frappe.whitelist()
def get_mop_from_payment_gateway(payment_gateway: str, company: str) -> str:
    """Get mode of payment associated with the given payment gateway"""
    mode_of_payment = None
    try:
        if not payment_gateway or not frappe.db.exists(
            "Payment Gateway", payment_gateway
        ):
            return None

        pg_accounts = frappe.get_all(
            "Payment Gateway Account",
            filters={"payment_gateway": payment_gateway},
            fields=["payment_account"],
        )

        if not pg_accounts:
            return None

        for pg_account in pg_accounts:
            payment_account = pg_account.payment_account

            mop_accounts = frappe.get_all(
                "Mode of Payment Account",
                filters={"default_account": payment_account, "company": company},
                fields=["parent"],
            )

            if mop_accounts:
                mode_of_payment = mop_accounts[0].parent
                break

    except Exception:
        frappe.log_error(frappe.get_traceback(), "get_mop_from_payment_gateway Error")
        pass

    return mode_of_payment


@frappe.whitelist()
def get_stk_amount(payment_name: str, company: str) -> float | None:
    """
    Determine valid amount for STK push based on currency rules.
    Returns amount or None if not allowed.
    """
    payment = frappe.get_doc("Sales Invoice Payment", payment_name)
    invoice = frappe.get_doc("Sales Invoice", payment.parent)
    company_currency = frappe.get_cached_value("Company", company, "default_currency")

    if invoice.currency == "KES":
        return payment.amount
    elif company_currency == "KES":
        return payment.base_amount
    else:
        return None
