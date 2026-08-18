import ast
import json

import frappe
import frappe.defaults
from frappe import _, qb
from frappe.utils import (
    flt,
    get_url_to_form,
    getdate,
    nowdate,
)

from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import (
    sanitize_mobile_number,
    submit_mpesa_payment,
)
from frappe_mpsa_payments.utils.doctype_names import MPESA_SETTINGS_DOCTYPE


def create_payment_entry(
    company,
    customer,
    amount,
    currency,
    mode_of_payment,
    party_type="Customer",
    reference_date=None,
    reference_no=None,
    posting_date=None,
    cost_center=None,
    submit=0,
    party_account=None,
    references=None,
):
    """
    Create a payment entry for a given customer and company.

    Args:
            company (str): Company for which the payment entry is being created.
            customer (str): Customer for whom the payment entry is being created.
            amount (float): Amount of the payment.
            currency (str): Currency of the payment.
            mode_of_payment (str): Mode of payment for the transaction.
            reference_date (str, optional): Reference date for the payment entry. Defaults to None.
            reference_no (str, optional): Reference number for the payment entry. Defaults to None.
            posting_date (str, optional): Posting date for the payment entry. Defaults to None.
            cost_center (str, optional): Cost center for the payment entry. Defaults to None.
            submit (int, optional): Whether to submit the payment entry immediately. Defaults to 0.

    Returns:
            PaymentEntry: Newly created payment entry document.
    """

    import erpnext
    from erpnext.accounts.doctype.bank_account.bank_account import (
        get_party_bank_account,
    )
    from erpnext.accounts.party import get_party_account
    from erpnext.accounts.utils import get_account_currency
    from erpnext.setup.utils import get_exchange_rate

    # TODO : need to have a better way to handle currency
    date = posting_date or nowdate()

    party_account = (
        party_account
        if party_account
        else get_party_account(party_type, customer, company)
    )
    if not party_account:
        frappe.throw(
            _(
                f"No party account found for {party_type} {customer} in company {company}. Please check the Party Account or the Company default settings."
            )
        )

    party_account_currency = get_account_currency(party_account)

    if party_account_currency != currency:
        frappe.throw(
            _(
                "Currency is not correct, party account currency is {party_account_currency} and transaction currency is {currency}"
            ).format(party_account_currency=party_account_currency, currency=currency)
        )

    payment_type = "Pay" if party_type in ["Employee", "Supplier"] else "Receive"

    bank = get_bank_cash_account(company, mode_of_payment)

    company_currency = frappe.get_value("Company", company, "default_currency")
    conversion_rate = get_exchange_rate(
        currency,
        company_currency,
        date,
        "for_selling" if payment_type == "Receive" else "for_buying",
    )
    paid_amount, received_amount = set_paid_amount_and_received_amount(
        party_account_currency, bank, amount, payment_type, None, conversion_rate
    )

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = payment_type
    pe.company = company
    pe.cost_center = cost_center or erpnext.get_default_cost_center(company)
    pe.posting_date = date
    pe.mode_of_payment = mode_of_payment
    pe.party_type = party_type
    pe.party = customer

    pe.paid_from = party_account if payment_type == "Receive" else bank.account
    pe.paid_to = party_account if payment_type == "Pay" else bank.account
    pe.paid_from_account_currency = (
        party_account_currency if payment_type == "Receive" else bank.account_currency
    )
    pe.paid_to_account_currency = (
        party_account_currency if payment_type == "Pay" else bank.account_currency
    )
    pe.paid_amount = paid_amount
    pe.received_amount = received_amount
    pe.letter_head = frappe.get_value("Company", company, "default_letter_head")
    pe.reference_date = reference_date
    pe.reference_no = reference_no
    if pe.party_type in ["Customer", "Supplier", "Employee"]:
        bank_account = get_party_bank_account(pe.party_type, pe.party)
        if bank_account:
            pe.set("bank_account", bank_account)
            pe.set_bank_account_data()

    pe.setup_party_account_field()
    pe.set_missing_values()
    pe.set_amounts()

    if references:
        for ref in references:
            reference_row = {
                "reference_doctype": ref["reference_doctype"],
                "reference_name": ref["reference_name"],
                "allocated_amount": ref["allocated_amount"],
            }
            if "b2c_payment_disbursement" in ref:
                reference_row["b2c_payment_disbursement"] = ref[
                    "b2c_payment_disbursement"
                ]

            pe.append("references", reference_row)

    pe.insert(ignore_permissions=True)

    if frappe.utils.cint(submit):
        pe.submit()

    return pe


def get_bank_cash_account(company, mode_of_payment, bank_account=None):
    """
    Retrieve the default bank or cash account based on the company and mode of payment.

    Args:
            company (str): Company for which the account is being retrieved.
            mode_of_payment (str): Mode of payment for the transaction.
            bank_account (str, optional): Specific bank account to retrieve. Defaults to None.

    Returns:
            BankAccount: Default bank or cash account.
    """
    from erpnext.accounts.doctype.journal_entry.journal_entry import (
        get_default_bank_cash_account,
    )

    bank = get_default_bank_cash_account(
        company, "Bank", mode_of_payment=mode_of_payment, account=bank_account
    )

    if not bank:
        bank = get_default_bank_cash_account(
            company, "Cash", mode_of_payment=mode_of_payment, account=bank_account
        )

    return bank


def set_paid_amount_and_received_amount(
    party_account_currency,
    bank,
    outstanding_amount,
    payment_type,
    bank_amount,
    conversion_rate,
):
    """
    Set the paid amount and received amount based on currency and conversion rate.

    Args:
            party_account_currency (str): Currency of the party account.
            bank (BankAccount): Bank account used for the transaction.
            outstanding_amount (float): Outstanding amount to be paid/received.
            payment_type (str): Type of payment (Receive/Pay).
            bank_amount (float): Amount in the bank account currency (if available).
            conversion_rate (float): Conversion rate between currencies.

    Returns:
            float: Paid amount.
            float: Received amount.
    """
    paid_amount = received_amount = 0
    if party_account_currency == bank["account_currency"]:
        paid_amount = received_amount = abs(outstanding_amount)
    elif payment_type == "Receive":
        paid_amount = abs(outstanding_amount)
        if bank_amount:
            received_amount = bank_amount
        else:
            received_amount = paid_amount * conversion_rate

    else:
        received_amount = abs(outstanding_amount)
        if bank_amount:
            paid_amount = bank_amount
        else:
            # if party account currency and bank currency is different then populate paid amount as well
            paid_amount = received_amount * conversion_rate

    return paid_amount, received_amount


@frappe.whitelist()
def get_outstanding_invoices(
    company,
    customer,
    invoice_type=None,
    common_filter=None,
    posting_date=None,
    from_date=None,
    to_date=None,
    min_outstanding=None,
    max_outstanding=None,
    accounting_dimensions=None,
    vouchers=None,
    limit=None,
    voucher_no=None,
):
    import erpnext
    from erpnext.accounts.party import get_party_account
    from erpnext.accounts.utils import QueryPaymentLedger

    if invoice_type is None:
        invoice_type = "Sales Invoice"

    account = (get_party_account("Customer", customer, company),)
    ple = qb.DocType("Payment Ledger Entry")
    outstanding_invoices = []
    precision = frappe.get_precision(invoice_type, "outstanding_amount") or 2

    if account:
        root_type, account_type = frappe.get_cached_value(
            "Account", account[0], ["root_type", "account_type"]
        )
        party_account_type = "Receivable" if root_type == "Asset" else "Payable"
        party_account_type = account_type or party_account_type
    else:
        party_account_type = erpnext.get_party_account_type("Customer")

    held_invoices = get_held_invoices("Customer", customer)

    common_filter = common_filter or []
    common_filter.append(ple.account_type == party_account_type)
    common_filter.append(ple.account.isin(account))
    common_filter.append(ple.party_type == "Customer")
    common_filter.append(ple.party == customer)
    if from_date and to_date:
        common_filter.append(ple.posting_date.between(from_date, to_date))
    elif from_date:
        common_filter.append(ple.posting_date >= from_date)
    elif to_date:
        common_filter.append(ple.posting_date <= to_date)

    ple_query = QueryPaymentLedger()
    invoice_list = ple_query.get_voucher_outstandings(
        vouchers=vouchers,
        common_filter=common_filter,
        posting_date=posting_date,
        min_outstanding=min_outstanding,
        max_outstanding=max_outstanding,
        get_invoices=True,
        accounting_dimensions=accounting_dimensions or [],
        limit=limit,
        voucher_no=voucher_no,
    )

    for d in invoice_list:
        payment_amount = (
            d.invoice_amount_in_account_currency - d.outstanding_in_account_currency
        )
        outstanding_amount = d.outstanding_in_account_currency
        if outstanding_amount > 0.5 / (10**precision):
            if (
                min_outstanding
                and max_outstanding
                and (
                    outstanding_amount < min_outstanding
                    or outstanding_amount > max_outstanding
                )
            ):
                continue

            if (
                d.voucher_type != "Purchase Invoice"
                or d.voucher_no not in held_invoices
            ):
                outstanding_invoices.append(
                    frappe._dict(
                        {
                            "voucher_no": d.voucher_no,
                            "voucher_type": d.voucher_type,
                            "posting_date": d.posting_date,
                            "invoice_amount": flt(d.invoice_amount_in_account_currency),
                            "payment_amount": payment_amount,
                            "outstanding_amount": outstanding_amount,
                            "due_date": d.due_date,
                            "currency": d.currency,
                            "account": d.account,
                        }
                    )
                )

    outstanding_invoices = sorted(
        outstanding_invoices, key=lambda k: k["due_date"] or getdate(nowdate())
    )
    return outstanding_invoices


def get_held_invoices(party_type, party):
    """
    Returns a list of names Purchase Invoices for the given party that are on hold
    """
    held_invoices = None

    if party_type == "Supplier":
        held_invoices = frappe.db.sql(
            "select name from `tabPurchase Invoice` where on_hold = 1 and release_date IS NOT NULL and release_date > CURDATE()",
            as_dict=1,
        )
        held_invoices = set(d["name"] for d in held_invoices)

    return held_invoices


@frappe.whitelist()
def get_unallocated_payments(customer, company, currency, mode_of_payment=None):
    """
    Retrieve unallocated payments for a given customer, company, and currency.

    Args:
            customer (str): Customer for whom payments are being retrieved.
            company (str): Company for which payments are being retrieved.
            currency (str): Currency of the payments.
            mode_of_payment (str, optional): Mode of payment for filtering payments. Defaults to None.

    Returns:
            list: List of unallocated payments.
    """
    filters = {
        "party": customer,
        "company": company,
        "docstatus": 1,
        "party_type": "Customer",
        "payment_type": "Receive",
        "unallocated_amount": [">", 0],
        "paid_from_account_currency": currency,
    }
    if mode_of_payment:
        filters.update({"mode_of_payment": mode_of_payment})
    unallocated_payment = frappe.get_all(
        "Payment Entry",
        filters=filters,
        fields=[
            "name",
            "paid_amount",
            "party_name as customer_name",
            "received_amount",
            "posting_date",
            "unallocated_amount",
            "mode_of_payment",
            "paid_from_account_currency as currency",
        ],
        order_by="posting_date asc",
    )
    return unallocated_payment


def get_total_amount_selected_mpesa_payments(selected_mpesa_payments):
    """
    Calculate the total amount of selected mpesa payments.

    Args:
            selected_mpesa_payments (list): List of selected mpesa payments.

    Returns:
            float: Total amount of selected mpesa payments.
    """
    total = 0
    for mpesa_payment in selected_mpesa_payments:
        doc = frappe.get_doc("Mpesa C2B Payment Register", mpesa_payment)
        total += flt(doc.get("transamount"))

    return total


def get_total_amount_selected_payments(invoice):
    """
    Calculate the total amount of selected payments.

    Args:
            selected_payments (list): List of selected payments.

    Returns:
            float: Total amount of selected payments.
    """
    total = 0
    doc = frappe.get_doc("POS Invoice", invoice)
    for payment in doc.payments:
        total += flt(payment.get("amount"))
    return total


def get_mode_of_payment(pos_profile):
    pos_doc = frappe.get_doc("POS Profile", pos_profile)
    for payment in pos_doc.payments:
        if payment.get("default") == 1:
            return payment.get("mode_of_payment")


@frappe.whitelist()
def get_available_pos_profiles(company, currency):
    """
    Retrieve available POS profiles for a given company and currency.

    Args:
            company (str): Company for which POS profiles are being retrieved.
            currency (str): Currency of the POS profiles.

    Returns:
            list: List of available POS profiles.
    """
    pos_profiles_list = frappe.get_list(
        "POS Profile",
        filters={"disabled": 0, "company": company, "currency": currency},
        page_length=1000,
        pluck="name",
    )
    return pos_profiles_list


def create_and_reconcile_payment_reconciliation(
    outstanding_invoices, customer, company, payment_entries
):
    from erpnext.accounts.party import get_party_account

    reconcile_doc = frappe.new_doc("Payment Reconciliation")
    reconcile_doc.party_type = "Customer"
    reconcile_doc.party = customer
    reconcile_doc.company = company
    reconcile_doc.receivable_payable_account = get_party_account(
        "Customer", customer, company
    )
    # reconcile_doc.get_unreconciled_entries()

    args = {
        "invoices": [],
        "payments": [],
    }

    for invoice in outstanding_invoices:
        invoice_name = (
            invoice.get("voucher_no") if isinstance(invoice, dict) else invoice
        )

        try:
            invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)

            invoice_data = {
                "invoice_type": "Sales Invoice",
                "invoice_number": invoice_doc.get("name"),
                "invoice_date": invoice_doc.get("posting_date"),
                "amount": invoice_doc.get("grand_total"),
                "outstanding_amount": invoice_doc.get("outstanding_amount"),
                "currency": invoice_doc.get("currency"),
                "exchange_rate": 0,
            }

            args["invoices"].append(invoice_data)

            reconcile_doc.append("invoices", invoice_data)

        except Exception as e:
            frappe.log_error(f"Failed to process invoice {invoice_name}: {str(e)}")
            continue

    for payment_entry in payment_entries:
        try:
            payment_entry_doc = frappe.get_doc("Payment Entry", payment_entry)

            payment_data = {
                "reference_type": "Payment Entry",
                "reference_name": payment_entry_doc.get("name"),
                "posting_date": payment_entry_doc.get("posting_date"),
                "amount": payment_entry_doc.get("unallocated_amount"),
                "unallocated_amount": payment_entry_doc.get("unallocated_amount"),
                "difference_amount": 0,
                "currency": payment_entry_doc.get("currency"),
                "exchange_rate": 0,
            }

            args["payments"].append(payment_data)
            reconcile_doc.append("payments", payment_data)

        except Exception as e:
            frappe.log_error(f"Failed to process payment {payment_entry}: {str(e)}")
            continue

    try:
        reconcile_doc.allocate_entries(args)
        reconcile_doc.reconcile()
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(f"Reconciliation failed: {str(e)}")
        frappe.throw(_("Reconciliation failed: {0}").format(str(e)))


@frappe.whitelist()
def process_mpesa_c2b_reconciliation(mpesa_names, invoice_names):
    if isinstance(mpesa_names, str):
        mpesa_names = json.loads(mpesa_names)
    if isinstance(invoice_names, str):
        invoice_names = json.loads(invoice_names)

    if not invoice_names:
        frappe.throw(_("No invoices provided."))

    first_invoice_name = invoice_names[0]
    first_invoice = frappe.get_doc("Sales Invoice", first_invoice_name)
    customer = first_invoice.get("customer")
    company = first_invoice.get("company")

    frappe.session.is_manual_reconciliation = True
    frappe.db.set_global("is_manual_reconciliation", "1")

    try:
        payment_entries = [
            submit_mpesa_payment(mpesa_name, customer).get("name")
            for mpesa_name in mpesa_names
        ]

        create_and_reconcile_payment_reconciliation(
            invoice_names, customer, company, payment_entries
        )

    finally:
        frappe.db.set_global("is_manual_reconciliation", "0")
        if hasattr(frappe.session, "is_manual_reconciliation"):
            del frappe.session.is_manual_reconciliation


@frappe.whitelist()
def process_mpesa_c2b_customer_credit():
    payment_entries_list = frappe.form_dict.get("payment_entries")
    payment_entries = ast.literal_eval(payment_entries_list)
    invoice_name = frappe.form_dict.get("invoice_name")
    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    customer = invoice.get("customer")
    company = invoice.get("company")

    create_and_reconcile_payment_reconciliation(
        invoice_name, customer, company, payment_entries
    )


@frappe.whitelist()
def initiate_payment_entry_stk(payment_entry):
    pe = frappe.get_doc("Payment Entry", payment_entry)
    pe.check_permission("submit")

    if pe.docstatus != 0:
        frappe.throw(_("STK push can only be initiated on a draft Payment Entry."))

    if pe.payment_type != "Receive" or pe.party_type != "Customer":
        frappe.throw(_("STK Push is only supported for incoming customer payments."))

    if not pe.get("custom_payment_gateway_account"):
        frappe.throw(_("Set the Payment Gateway Account before initiating STK Push."))

    if pe.difference_amount:
        frappe.throw(
            _(
                "Difference Amount must be zero before initiating an STK Push, otherwise "
                "the Payment Entry will not be submittable once the payment is received."
            )
        )

    # Get the mpesa settings record from the gateway account, and confirm it is an Mpesa gateway
    payment_gateway = frappe.db.get_value(
        "Payment Gateway Account", pe.custom_payment_gateway_account, "payment_gateway"
    )

    gateway_meta = frappe.db.get_value(
        "Payment Gateway",
        payment_gateway,
        ["gateway_settings", "gateway_controller"],
        as_dict=True,
    )
    if not gateway_meta or not gateway_meta.gateway_controller:
        frappe.throw(
            _("Could not resolve Mpesa Settings from that Payment Gateway Account.")
        )

    if gateway_meta.gateway_settings != MPESA_SETTINGS_DOCTYPE:
        frappe.throw(
            _("The selected Payment Gateway Account is not configured for M-Pesa.")
        )

    settings = gateway_meta.gateway_controller

    phone_number = pe.get("custom_mpesa_phone_number")
    if not phone_number and pe.party:
        phone_number = frappe.db.get_value("Customer", pe.party, "mobile_no")

    if not phone_number:
        frappe.throw(_("No M-Pesa phone number on the Payment Entry or the Customer."))

    phone_number = sanitize_mobile_number(phone_number)

    # Where the stkpush page should send the user once the payment is
    # reconciled and the Payment Entry has been submitted.
    redirect_to = get_url_to_form("Payment Entry", pe.name)

    # idempotency so that never create a second prompt for a Payment Entry that is
    # already paid or has a live/failed request. Instead, route the user back
    # to the existing request's status page, where they can watch progress or
    # retry (with an editable phone number) if it failed.
    existing = frappe.db.get_value(
        "Mpesa Express Request",
        {
            "reference_doctype": "Payment Entry",
            "reference_name": pe.name,
            "status": ["in", ["In Progress", "Completed", "Failed"]],
        },
        ["name", "status", "route", "redirect_to"],
        as_dict=True,
    )

    if existing:
        if existing.status == "Completed":
            frappe.throw(
                _(
                    "An M-Pesa payment for this Payment Entry has already been "
                    "completed (request {0})."
                ).format(existing.name)
            )

        # In Progress or Failed: refresh the request with the latest details
        # from the Payment Entry so a retry from the status page uses them.
        updates = {}
        if not existing.redirect_to:
            updates["redirect_to"] = redirect_to
        if existing.status == "Failed":
            updates["phone_number"] = phone_number
            updates["amount"] = pe.paid_amount
        if updates:
            frappe.db.set_value("Mpesa Express Request", existing.name, updates)

        return {"name": existing.name, "route": existing.route}

    request = frappe.get_doc(
        {
            "doctype": "Mpesa Express Request",
            "settings": settings,
            "reference_doctype": "Payment Entry",
            "reference_name": pe.name,
            "amount": pe.paid_amount,
            "phone_number": phone_number,
            "redirect_to": redirect_to,
        }
    )
    request.insert(ignore_permissions=True)
    request.submit()  # on_submit sends the stkpush
    return {"name": request.name, "route": request.route}
