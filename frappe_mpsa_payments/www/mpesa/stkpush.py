import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import now


def get_context(context):
    frappe.local.response["headers"] = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }
    q = frappe.local.form_dict
    request_id = q.get("id")
    if not request_id:
        frappe.throw(_("M-Pesa Request ID is required"))
    context.is_new = False
    doc = load_existing(request_id)
    context.mpesa_request = build_mpesa_context(doc)
    context.allow_phone_edit = should_allow_phone_edit(doc)
    context.allow_amount_editing = False
    context.allow_payment_reference_editing = False
    context.is_failed = doc.status == "Failed"
    context.is_completed = doc.status == "Completed"
    context.is_pending = doc.status in ("Pending", "Initiated", "In Progress")
    context.docstatus = doc.docstatus

    if context.is_completed:
        context.success = _("Payment completed successfully")
    if context.is_failed:
        # result_desc is the outcome Safaricom sent back ("Request Cancelled by
        # user"). response_description only says whether the push was accepted
        # for delivery, so on a failure it reads "Success..." and tells the
        # payer nothing about why their payment did not go through.
        context.error = _("Payment failed: {0}").format(
            doc.result_desc or doc.response_description or _("Unknown error")
        )
    context.cache_buster = now()


def load_existing(request_id):
    try:
        return frappe.get_doc("Mpesa Express Request", {"request_id": request_id})
    except frappe.DoesNotExistError:
        frappe.throw(_("M-Pesa request not found"))


def build_mpesa_context(doc):
    return {
        "name": doc.name,
        "request_id": doc.request_id,
        "phone_number": clean(doc.phone_number),
        "payment_gateway": doc.payment_gateway,
        "reference_type": doc.reference_doctype,
        "reference_id": doc.reference_name,
        "base_amount": getattr(doc, "base_amount", doc.amount),
        "amount": doc.amount,
        "currency": getattr(doc, "currency", "KES"),
        "status": doc.status,
        "checkout_request_id": doc.checkout_request_id,
        "response_description": doc.response_description,
        "title": doc.transaction_title,
        "description": doc.transaction_description,
        "docstatus": doc.docstatus,
    }


def should_allow_phone_edit(doc):
    return doc.docstatus == 0 or doc.status == "Failed"


def clean(value):
    if value in (None, "null", "", "undefined"):
        return ""
    return value


# These three are reachable without logging in, because the payer is a
# customer following a link, not a user. The request_id in that link is the
# only credential they hold: 32 hex characters from frappe.generate_hash.
#
# They must therefore be addressed by request_id and never by document name.
# Names are sequential (MEXP-26-08-000004), so accepting one would let anyone
# walk the series and fire pushes against other people's requests.


@frappe.whitelist(allow_guest=True)
@rate_limit(key="request_id", limit=5, seconds=60)
def initiate_stk_push(request_id, phone_number):
    doc = load_existing(request_id)
    doc.flags.ignore_permissions = True
    doc.phone_number = phone_number
    doc.save(ignore_permissions=True)
    doc.submit()
    return {"message": "STK Push initiated"}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="request_id", limit=5, seconds=60)
def retry_stkpush(request_id, phone_number):
    doc = load_existing(request_id)
    doc.flags.ignore_permissions = True
    doc.phone_number = phone_number
    doc.status = "In Progress"
    doc.save(ignore_permissions=True)
    doc.initiate_request()
    return {"message": "STK Push initiated"}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="request_id", limit=120, seconds=60)
def check_payment_status(request_id):
    doc = load_existing(request_id)

    # For Payment Entries, redirect only after reconciliation. This prevents
    # a transaction status query from redirecting to a draft Payment Entry.
    redirect_to = doc.redirect_to
    if doc.reference_doctype == "Payment Entry" and not doc.is_reconciled:
        redirect_to = None

    return {
        "status": doc.status,
        "docstatus": doc.docstatus,
        # Both: result_* is the outcome, response_description only covers
        # whether Safaricom accepted the push for delivery.
        "result_code": doc.result_code,
        "result_desc": doc.result_desc,
        "response_description": doc.response_description,
        "redirect_to": redirect_to,
    }
