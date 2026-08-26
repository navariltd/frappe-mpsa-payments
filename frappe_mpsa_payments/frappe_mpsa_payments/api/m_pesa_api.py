from __future__ import unicode_literals

import base64
import datetime
import json
import time
from typing import Any

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt
from requests.auth import HTTPBasicAuth

from ...utils.doctype_names import MPESA_EXPRESS_REQUEST_DOCTYPE, MPESA_SETTINGS_DOCTYPE
from ...utils.encoding_initiator_password import (
    generate_security_credential,
)
from ...utils.utils import (
    build_callback_url,
    log_and_throw_error,
    update_mpesa_request_status,
)
from .mpesa_response_handler import (
    BULK_PULL_BATCH_FLAG,
    BULK_PULL_FLAG,
    BULK_PULL_RESULTS_FLAG,
    balance_query_on_success,
    publish_pull_result,
    pull_transaction_on_error,
    pull_transaction_on_success,
    record_pull_outcome,
    stk_push_on_error,
    stk_push_on_success,
    transaction_status_on_success,
)
from .process_request import process_request


@frappe.whitelist(allow_guest=True)
def balance_query_callback(**kwargs) -> None:
    import json

    args = frappe._dict(kwargs)
    result_data = args.get("Result")
    if not result_data:
        frappe.log_error(
            "M-Pesa Balance Query Error", "Missing 'Result' in callback response"
        )
        return

    result_code = result_data.get("ResultCode")
    result_desc = result_data.get("ResultDesc", "No description provided")

    if result_code is None:
        frappe.log_error(
            "M-Pesa Balance Query Error", "Missing 'ResultCode' in callback response"
        )
        return

    conversation_id = result_data.get("ConversationID")
    if not conversation_id:
        frappe.log_error(
            "M-Pesa Balance Query Error", "ConversationID missing in callback response"
        )
        return

    integration_request = frappe.get_list(
        "Integration Request",
        filters=[["output", "like", f"%{conversation_id}%"]],
        fields=["name", "output", "reference_docname"],
        ignore_permissions=True,
    )

    if not integration_request:
        frappe.log_error(
            "M-Pesa Balance Query Error",
            f"No matching Integration Request found for ConversationID: {conversation_id}",
        )
        return

    request_doc = frappe.get_doc("Integration Request", integration_request[0].name)
    request_doc.flags.ignore_permissions = True

    if str(result_code) != "0":
        request_doc.status = "Failed"
        request_doc.error = json.dumps(result_data, indent=4)
        request_doc.save(ignore_permissions=True)
        frappe.log_error(
            title="M-Pesa Balance Query Error",
            message=f"ResultCode: {result_code}, ResultDesc: {result_desc}, Data: {json.dumps(result_data, indent=4)}",
        )
        return

    request_doc.output = json.dumps(result_data, indent=4)
    request_doc.status = "Completed"
    request_doc.save(ignore_permissions=True)

    account_balance = None
    result_params = result_data.get("ResultParameters", {}).get("ResultParameter", [])
    for param in result_params:
        if param.get("Key") == "AccountBalance":
            account_balance = param.get("Value")
            break

    if not account_balance:
        frappe.log_error(
            "M-Pesa Balance Query Error", "AccountBalance missing in callback response"
        )
        return

    settings_docname = integration_request[0].get("reference_docname")
    if not settings_docname:
        frappe.log_error(
            "M-Pesa Balance Query Error",
            "Reference document name missing in Integration Request",
        )
        return

    settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, settings_docname)
    settings.flags.ignore_permissions = True

    if account_balance:
        frappe.db.set_value(
            MPESA_SETTINGS_DOCTYPE, settings.name, "account_balance", account_balance
        )

    frappe.publish_realtime(
        event="refresh_form", doctype=MPESA_SETTINGS_DOCTYPE, docname=settings_docname
    )


@frappe.whitelist()
def get_account_balance(name: str) -> Any:
    """Call account balance API to send the request to the Mpesa Servers."""
    try:
        settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, name)
        certs = frappe.get_single("Mpesa Public Key Certificate")
        cert_url = ""

        if settings.sandbox:
            cert_url = certs.sandbox_certificate
        else:
            cert_url = certs.production_certificate

        security_credential = generate_security_credential(
            (
                settings.get_password("initiator_password", "")
                if settings.initiator_password
                else ""
            ),
            cert_url,
            settings.sandbox,
        )

        endpoint = "/mpesa/accountbalance/v1/query"

        callback_url = build_callback_url(
            "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.balance_query_callback"
        )
        timeout_url = build_callback_url(
            "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.handle_queue_timeout"
        )

        payload = {
            "Initiator": settings.initiator_name,
            "SecurityCredential": settings.get_password("security_credential")
            or security_credential,
            "CommandID": "AccountBalance",
            "PartyA": settings.business_shortcode,
            "IdentifierType": "4",
            "Remarks": "Balance",
            "QueueTimeOutURL": timeout_url,
            "ResultURL": callback_url,
        }

        response = process_request(
            endpoint=endpoint,
            method="POST",
            payload=payload,
            success_callback=balance_query_on_success,
            request_description="Mpesa Balance Query",
            doctype=MPESA_SETTINGS_DOCTYPE,
            document_name=name,
            settings_name=name,
        )
        return response

    except Exception:
        frappe.log_error(frappe.get_traceback(), "Mpesa Balance Query Error")
        frappe.log_error(
            _("Failed to check mpesa balance. Please check the error logs.")
        )


@frappe.whitelist()
def check_transaction_status(name: str) -> Any:
    """Check the status of a transaction by its name."""
    try:
        express_request = frappe.get_doc(MPESA_EXPRESS_REQUEST_DOCTYPE, name)
        settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, express_request.settings)

        endpoint = "/mpesa/stkpushquery/v1/query"
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        payload = {
            "BusinessShortCode": settings.business_shortcode,
            "Password": generate_request_password(settings, timestamp),
            "Timestamp": timestamp,
            "CheckoutRequestID": express_request.checkout_request_id,
        }

        response = process_request(
            endpoint=endpoint,
            method="POST",
            payload=payload,
            success_callback=transaction_status_on_success,
            error_callback=transaction_status_error_callback,
            request_description="Mpesa Transaction Status Query",
            doctype=MPESA_EXPRESS_REQUEST_DOCTYPE,
            document_name=express_request.name,
            settings_name=express_request.settings,
            reuse_existing_request=True,
        )
        return response

    except Exception:
        frappe.log_error(frappe.get_traceback(), "STK Push Query Error")
        frappe.log_error(
            _("Failed to check transaction status. Please check the error logs.")
        )


def generate_request_password(settings: Document, timestamp: str) -> str:
    """Generate the password for making a request to the M-Pesa API."""
    shortcode = str(settings.business_shortcode).strip()
    passkey = str(settings.get_password("online_passkey")).strip()
    data_to_encode = f"{shortcode}{passkey}{timestamp}"
    return base64.b64encode(data_to_encode.encode("utf-8")).decode("utf-8")


def transaction_status_error_callback(
    response: dict, payload: dict, document_name: str, **kwargs
) -> None:
    """Mark transaction as failed immediately if query fails."""
    frappe.log_error(
        message=f"Transaction {document_name} query failed.\nResponse: {frappe.as_json(response)}",
        title="Mpesa Transaction Status Query Error",
    )

    frappe.db.set_value(
        MPESA_EXPRESS_REQUEST_DOCTYPE,
        document_name,
        {
            "status": "Failed",
            "result_desc": (
                response.get("errorMessage")
                if isinstance(response, dict)
                else "Unknown error"
            ),
        },
    )


@frappe.whitelist()
def initiate_stk_push(**args) -> any:
    """Generate STK push by making an API call to the STK push API."""

    # If args is a single key "args" containing a JSON string, parse it
    if len(args) == 1 and "args" in args:
        try:
            parsed_args = json.loads(args.get("args"))
            if isinstance(parsed_args, dict):
                args = frappe._dict(parsed_args)
            else:
                frappe.log_error(_("Invalid input format. Expected JSON object."))
        except json.JSONDecodeError:
            frappe.log_error(_("Failed to decode JSON arguments."))
    else:
        args = frappe._dict(args)

    required_fields = ["payment_gateway", "phone_number", "request_amount"]
    missing_fields = [field for field in required_fields if not args.get(field)]
    if missing_fields:
        frappe.log_error(
            _("Missing required fields: {0}").format(", ".join(missing_fields))
        )

    try:
        callback_url = build_callback_url(
            "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.stk_push_callback"
        )
        mpesa_settings = frappe.get_doc(
            MPESA_SETTINGS_DOCTYPE, args.payment_gateway[6:]
        )
        mobile_number = sanitize_mobile_number(args.phone_number or args.sender)
        amount = args.request_amount
        business_shortcode = mpesa_settings.business_shortcode
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        reference_name = args.get("reference_name", "Online Payment")

        payload = {
            "BusinessShortCode": business_shortcode,
            "Password": generate_request_password(mpesa_settings, timestamp),
            "Timestamp": timestamp,
            "Amount": amount,
            "PartyA": int(mobile_number),
            "PartyB": (
                business_shortcode
                if mpesa_settings.paybill_type == "Pay Bill"
                else mpesa_settings.till_number
            ),
            "PhoneNumber": int(mobile_number),
            "CallBackURL": callback_url,
            "AccountReference": reference_name,
            "TransactionDesc": reference_name,
            "TransactionType": (
                "CustomerPayBillOnline"
                if mpesa_settings.paybill_type == "Pay Bill"
                else "CustomerBuyGoodsOnline"
            ),
        }

        endpoint = "/mpesa/stkpush/v1/processrequest"

        response = process_request(
            endpoint=endpoint,
            method="POST",
            payload=payload,
            success_callback=stk_push_on_success,
            error_callback=stk_push_on_error,
            request_description="Mpesa STK Push",
            doctype=args.get("doctype", MPESA_SETTINGS_DOCTYPE),
            document_name=args.get("document_name", mpesa_settings.name),
            settings_name=mpesa_settings.name,
        )
        return response

    except Exception:
        frappe.log_error(frappe.get_traceback(), "STK Push Generation Error")
        frappe.log_error(_("Failed to generate STK push. Please check the error logs."))


@frappe.whitelist(allow_guest=True)
def stk_push_callback(**kwargs) -> None:
    frappe.set_user("Administrator")

    try:
        transaction_response = frappe._dict(kwargs["Body"]["stkCallback"])
        checkout_request_id = transaction_response.get("CheckoutRequestID")
        if not isinstance(checkout_request_id, str):
            log_and_throw_error("Invalid Checkout Request ID")

        result_code = transaction_response.get("ResultCode")
        status = "Completed" if str(result_code) == "0" else "Failed"

        callback_metadata = transaction_response.get("CallbackMetadata", {}).get(
            "Item", []
        )
        metadata_dict = {
            item.get("Name"): item.get("Value")
            for item in callback_metadata
            if "Value" in item
        }

        transaction_date = metadata_dict.get("TransactionDate")
        if transaction_date:
            if not isinstance(transaction_date, str):
                transaction_date = str(transaction_date)

            date_obj = datetime.datetime.strptime(transaction_date, "%Y%m%d%H%M%S")

            metadata_dict["TransactionDate"] = date_obj

        request_doc = frappe.get_doc(
            MPESA_EXPRESS_REQUEST_DOCTYPE, {"checkout_request_id": checkout_request_id}
        )

        update_mpesa_request_status(
            request_doc.name,
            {
                "result_code": result_code,
                "result_desc": transaction_response.get("ResultDesc"),
                "transaction_id": metadata_dict.get("MpesaReceiptNumber"),
                "transaction_date": metadata_dict.get("TransactionDate"),
                "status": status,
            },
        )

        realtime_payload = {
            "status": status,
            "request_name": request_doc.name,
            "reference_doctype": request_doc.reference_doctype,
            "reference_name": request_doc.reference_name,
            "transaction_id": metadata_dict.get("MpesaReceiptNumber"),
            "amount": request_doc.amount,
            "result_desc": transaction_response.get("ResultDesc"),
        }
        frappe.publish_realtime(
            event="mpesa_stk_payment_completed",
            message=realtime_payload,
            user=request_doc.owner,
        )
        frappe.publish_realtime(
            event="mpesa_stk_payment_completed",
            message=realtime_payload,
        )

        request_doc.validate_duplicate_c2b_records()

        integration_req = frappe.get_doc(
            "Integration Request", {"output": ["like", f"%{checkout_request_id}%"]}
        )
        integration_req.flags.ignore_permissions = True
        current_output = integration_req.output or "{}"
        output = json.dumps(
            {
                "stkpush_response": current_output,
                "callback_result": transaction_response,
            },
            indent=4,
        )

        frappe.db.set_value(
            "Integration Request",
            integration_req.name,
            {
                "status": status,
                "output": output,
            },
        )

        if status == "Completed":
            request_doc.reconcile_payment()

        if "erpnext" in frappe.get_installed_apps():
            if request_doc and request_doc.reference_doctype == "Payment Request":
                publish_stk_status(status, request_doc.reference_name)

    except Exception:
        log_and_throw_error("STK Push Callback Error", checkout_request_id)


def publish_stk_status(status: str, payment_request):
    expected_token = frappe.db.get_value(
        "Payment Request", payment_request, "payment_token"
    )

    if not expected_token:
        return

    room = "website"

    frappe.publish_realtime(
        "stk_payment_complete",
        {
            "status": status,
            "expected_token": expected_token,
        },
        room=room,
    )


def sanitize_mobile_number(number: str) -> str:
    """Strip all non-digit characters, take the last 9 digits, and add country code."""
    sanitized_number = "".join(filter(str.isdigit, number))[-9:]
    return "254" + sanitized_number


def get_token(app_key, app_secret, base_url):
    authenticate_uri = "/oauth/v1/generate?grant_type=client_credentials"
    authenticate_url = f"{base_url.rstrip('/')}{authenticate_uri}"

    credentials = f"{app_key}:{app_secret}"
    encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

    r = requests.get(
        authenticate_url,
        headers={
            "Authorization": f"Basic {encoded_credentials}",
        },
        timeout=30,
    )

    try:
        data = r.json()
    except requests.exceptions.JSONDecodeError:
        frappe.throw(
            f"M-PESA returned an invalid response. "
            f"Status: {r.status_code}, "
            f"Response: {r.text[:500]}"
        )

    if not r.ok:
        frappe.throw(
            f"M-PESA authentication failed. "
            f"Status: {r.status_code}, "
            f"Response: {data}"
        )

    access_token = data.get("access_token")

    if not access_token:
        frappe.throw(
            f"M-PESA authentication response did not include an access token: {data}"
        )

    return access_token


@frappe.whitelist(allow_guest=True)
def confirmation(**kwargs):
    try:
        args = frappe._dict(kwargs)

        frappe.set_user("Administrator")

        frappe.enqueue(
            "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.delayed_insert_c2b",
            queue="short",
            timeout=300,
            is_async=True,
            c2b_data={
                "transactiontype": args.get("TransactionType"),
                "transid": args.get("TransID"),
                "transtime": args.get("TransTime"),
                "transamount": flt(args.get("TransAmount")),
                "businessshortcode": args.get("BusinessShortCode"),
                "billrefnumber": args.get("BillRefNumber"),
                "invoicenumber": args.get("InvoiceNumber"),
                "orgaccountbalance": args.get("OrgAccountBalance"),
                "thirdpartytransid": args.get("ThirdPartyTransID"),
                "msisdn": args.get("MSISDN"),
                "firstname": args.get("FirstName"),
                "middlename": args.get("MiddleName"),
                "lastname": args.get("LastName"),
            },
        )

        frappe.db.commit()
        context = {"ResultCode": 0, "ResultDesc": "Accepted"}
        return dict(context)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), str(e)[:140])
        context = {"ResultCode": 1, "ResultDesc": "Rejected"}
        return dict(context)
    finally:
        frappe.set_user("Guest")


@frappe.whitelist(allow_guest=True)
def validation(**kwargs):
    context = {"ResultCode": 0, "ResultDesc": "Accepted"}
    return dict(context)


def delayed_insert_c2b(c2b_data: dict) -> None:
    """
    Insert C2B Payment Register after a small delay.
    This ensures that any Express Request with the same transaction_id
    is already created and can be prioritized.
    """
    try:
        time.sleep(1)

        if c2b_data.get("transid"):
            express_exists = frappe.db.exists(
                "Mpesa Express Request", {"transaction_id": c2b_data["transid"]}
            )
            if express_exists:
                frappe.log_error(
                    f"C2B {c2b_data['transid']} blocked due to existing Express Request",
                    "C2B Insert Skipped",
                )
                return

        doc = frappe.new_doc("Mpesa C2B Payment Register")
        for k, v in c2b_data.items():
            setattr(doc, k, v)

        doc.insert(ignore_permissions=True)
        frappe.db.commit()

    except Exception:
        frappe.log_error(frappe.get_traceback(), "Delayed C2B Insert Error")


@frappe.whitelist()
def get_mpesa_mode_of_payment(company):
    modes = frappe.get_all(
        "Mpesa C2B Payment Register URL",
        filters={"company": company, "register_status": "Success"},
        fields=["mode_of_payment"],
    )
    modes_of_payment = []
    for mode in modes:
        if mode.mode_of_payment not in modes_of_payment:
            modes_of_payment.append(mode.mode_of_payment)
    return modes_of_payment


@frappe.whitelist(allow_guest=True)
def get_mpesa_draft_c2b_payments(
    company,
    full_name=None,
    mode_of_payment=None,
    from_date=None,
    to_date=None,
):
    fields = [
        "name",
        "transid",
        "company",
        "msisdn",
        "full_name",
        "posting_date",
        "posting_time",
        "transamount",
    ]

    filters = {"company": company, "docstatus": 0}
    order_by = "posting_date desc, posting_time desc"

    if mode_of_payment:
        filters["mode_of_payment"] = mode_of_payment

    if full_name:
        filters["full_name"] = ["like", f"%{full_name}%"]

    if from_date and to_date:
        filters["posting_date"] = ["between", [from_date, to_date]]
    elif from_date:
        filters["posting_date"] = [">=", from_date]
    elif to_date:
        filters["posting_date"] = ["<=", to_date]

    payments = frappe.get_all(
        "Mpesa C2B Payment Register", filters=filters, fields=fields, order_by=order_by
    )

    return payments


@frappe.whitelist(allow_guest=True)
def get_draft_pos_invoice(search_term=None):
    from frappe import qb
    from frappe.query_builder import DocType

    SalesInvoice = DocType("Sales Invoice")
    fields = ["*"]
    status_filters = [
        "Overdue",
        "Partially Paid",
        "Unpaid",
        "Overdue and Discounted",
        "Partially Paid and Discounted",
    ]

    # Create the base query
    query = (
        qb.from_(SalesInvoice)
        .select(*fields)
        .where(SalesInvoice.docstatus == 1)
        .where(SalesInvoice.status.isin(status_filters))
        .orderby(SalesInvoice.posting_date, order=qb.desc)
    )

    if search_term:
        search_filter = (SalesInvoice.customer.like(f"%{search_term}%")) | (
            SalesInvoice.name.like(f"%{search_term}%")
        )
        query = query.where(search_filter)

    invoices = query.run(as_dict=True)

    frappe.response["message"] = invoices


@frappe.whitelist()
def submit_mpesa_payment(mpesa_payment, customer):
    try:
        doc = process_mpesa_payment(mpesa_payment, customer, submit_payment=True)
        return frappe.get_doc("Payment Entry", doc.payment_entry)
    except Exception as e:
        frappe.log_error(f"Error: {str(e)}", "submit_mpesa_payment")
        raise


@frappe.whitelist()
def submit_instant_mpesa_payment():
    mpesa_payment = frappe.form_dict.get("mpesa_payment")
    customer = None
    if "erpnext" in frappe.get_installed_apps():
        customer = frappe.form_dict.get("customer")

    try:
        process_mpesa_payment(mpesa_payment, customer, submit_payment=False)
    except Exception as e:
        frappe.log_error(f"Error: {str(e)}", "submit_instant_mpesa_payment")
        raise


def process_mpesa_payment(mpesa_payment, customer, submit_payment=False):
    try:
        doc = frappe.get_doc("Mpesa C2B Payment Register", mpesa_payment)
        if "erpnext" in frappe.get_installed_apps():
            doc.customer = customer
            # TODO: after testing, mode of payment
            doc.mode_of_payment = get_mode_of_payment(doc)
        doc.submit_payment = submit_payment
        doc.save()
        doc.submit()
        frappe.db.commit()

        doc.reload()

        return doc
    except Exception as e:
        frappe.log_error(f"Error: {str(e)}", "process_mpesa_payment")
        raise


def get_payment_method(pos_profile):
    pos_profile_doc = frappe.get_doc("POS Profile", pos_profile)
    for payment in pos_profile_doc.payments:
        if payment.default == 1:
            return payment.mode_of_payment
    return None


def get_mode_of_payment(mpesa_doc):
    business_short_code = mpesa_doc.businessshortcode
    mode_of_payment = frappe.get_value(
        "Mpesa C2B Payment Register URL",
        {"business_shortcode": business_short_code, "register_status": "Success"},
        "mode_of_payment",
    )
    if mode_of_payment is None:
        mode_of_payment = frappe.get_value(
            "Mpesa C2B Payment Register URL",
            {"till_number": business_short_code, "register_status": "Success"},
            "mode_of_payment",
        )
    return mode_of_payment


@frappe.whitelist(allow_guest=True)
def handle_transaction_status_result():
    """Handle the transaction status response from Mpesa"""
    try:
        response = frappe.request.data
        response_data = json.loads(response)

        integration_request = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "is_remote_request": 1,
                "integration_request_service": "Mpesa Transaction Status Result Callback",
                "reference_doctype": "Mpesa C2B Payment Register",
                "status": "Queued",
                "data": json.dumps(response_data),
                "url": frappe.request.url,
                "method": "POST",
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.enqueue(
            "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_mpesa_integration_request",
            queue="short",
            timeout=300,
            job_id=f"mpesa_process_{integration_request.name}",
            integration_request_name=integration_request.name,
            deduplicate=True,
        )

        return {"status": "queued", "message": "Transaction queued for processing"}

    except json.JSONDecodeError as e:
        frappe.log_error(
            f"Failed to decode JSON from Mpesa response: {str(e)}", "Mpesa API Error"
        )
        return {"status": "error", "message": "Invalid JSON data"}
    except Exception as e:
        frappe.log_error(f"Error in Mpesa webhook: {str(e)}", "Mpesa API Error")
        return {"status": "error", "message": f"Webhook error: {str(e)}"}


def process_mpesa_integration_request(integration_request_name):
    """Process the Mpesa Integration Request and publish updates in real-time"""
    try:
        # Fetch the Integration Request
        integration_request = frappe.get_doc(
            "Integration Request", integration_request_name
        )

        # Parse the stored data
        response_data = json.loads(integration_request.data)
        result_data = response_data.get("Result", {})
        result_parameters = result_data.get("ResultParameters", {}).get(
            "ResultParameter", []
        )
        result_params = {
            param.get("Key", ""): param.get("Value", "")
            for param in result_parameters
            if "Key" in param
        }

        result_code = result_data.get("ResultCode", None)
        receipt_no = result_params.get("ReceiptNo", "")
        business_shortcode = result_params.get("CreditPartyName", "").split("-")

        if result_code == 0:
            if frappe.db.exists("Mpesa C2B Payment Register", {"transid": receipt_no}):
                error_msg = (
                    f"Duplicate transaction: Receipt No {receipt_no} already exists"
                )
                integration_request.status = "Failed"
                integration_request.output = error_msg
                integration_request.save(ignore_permissions=True)
                frappe.db.commit()

                frappe.publish_realtime(
                    event="mpesa_transaction_status",
                    message={"status": "error", "message": error_msg},
                    user=frappe.session.user,
                )
                return

            # Create the Mpesa document
            mpesa_doc = frappe.new_doc("Mpesa C2B Payment Register")
            mpesa_doc.full_name = result_params.get("DebitPartyName", "")
            mpesa_doc.transactiontype = result_params.get("ReasonType", "")
            mpesa_doc.transid = result_params.get("ReceiptNo", "")
            mpesa_doc.transtime = result_params.get("InitiatedTime", "")
            mpesa_doc.transamount = float(result_params.get("Amount", 0.0))
            mpesa_doc.businessshortcode = business_shortcode[0]
            mpesa_doc.billrefnumber = result_params.get("ReceiptNo", "")
            mpesa_doc.invoicenumber = result_params.get("TransactionID", "")
            mpesa_doc.orgaccountbalance = result_params.get("DebitAccountType", "")
            mpesa_doc.thirdpartytransid = result_params.get(
                "OriginatorConversationID", ""
            )

            debit_party = result_params.get("DebitPartyName", "").split(" - ")
            mpesa_doc.msisdn = debit_party[0] if len(debit_party) > 0 else ""
            name_parts = (
                debit_party[1].split(" ") if len(debit_party) > 1 else ["", "", ""]
            )
            mpesa_doc.firstname = name_parts[0]
            mpesa_doc.middlename = name_parts[1] if len(name_parts) > 1 else ""
            mpesa_doc.lastname = name_parts[-1] if len(name_parts) > 2 else ""

            mpesa_doc.insert(ignore_permissions=True)
            frappe.db.commit()

            success_msg = "Transaction processed successfully"
            integration_request.status = "Completed"
            integration_request.output = success_msg
            integration_request.reference_document = mpesa_doc.name
            integration_request.save(ignore_permissions=True)
            frappe.db.commit()

            frappe.publish_realtime(
                event="mpesa_transaction_status",
                message={
                    "status": "success",
                    "message": success_msg,
                    "doc_name": mpesa_doc.name,
                },
                user=frappe.session.user,
            )

        else:
            error_msg = "Transaction failed with non-zero result code"
            integration_request.status = "Failed"
            integration_request.output = error_msg
            integration_request.save(ignore_permissions=True)
            frappe.db.commit()

            frappe.publish_realtime(
                event="mpesa_transaction_status",
                message={"status": "error", "message": error_msg},
                user=frappe.session.user,
            )

    except Exception as e:
        error_message = f"Mpesa Processing Error: {str(e)}"
        integration_request.status = "Failed"
        integration_request.output = error_message
        integration_request.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.log_error(
            f"{error_message}\nData: {integration_request.data}",
            "Mpesa Integration Error",
        )
        frappe.publish_realtime(
            event="mpesa_transaction_status",
            message={"status": "error", "message": error_message},
            user=frappe.session.user,
        )


@frappe.whitelist(allow_guest=True)
def handle_queue_timeout():
    """Handle the timeout response from Mpesa."""
    try:
        response = frappe.request.data
        response_data = json.loads(response)

        frappe.log_error(
            title="Mpesa Queue Timeout",
            message=f"Timeout response received: {frappe.as_json(response_data)}",
        )

        return {"status": "timeout", "message": "Timeout response logged successfully."}

    except json.JSONDecodeError:
        frappe.log_error(
            title="Mpesa Timeout Error",
            message="Failed to decode JSON from timeout response.",
        )
        return {"status": "error", "message": "Invalid JSON received."}

    except Exception as e:
        error_message = f"Mpesa Timeout Error: {str(e)}"
        frappe.log_error(title="Mpesa Timeout Error", message=error_message)
        return {"status": "error", "message": str(e)}


@frappe.whitelist(allow_guest=True)
def verify_transaction(**kwargs) -> None:
    """Verify the transaction result received via callback from stk."""
    from ..doctype.mpesa_settings.mpesa_settings import (
        fetch_param_value,
        get_completed_integration_requests_info,
    )

    transaction_response = frappe._dict(kwargs["Body"]["stkCallback"])

    checkout_id = getattr(transaction_response, "CheckoutRequestID", "")
    if not isinstance(checkout_id, str):
        frappe.log_error(_("Invalid Checkout Request ID"))

    integration_request = frappe.get_doc("Integration Request", checkout_id)
    transaction_data = frappe._dict(json.loads(integration_request.data))
    total_paid = 0
    success = False  # for reporting successfull callback to point of sale ui

    if transaction_response["ResultCode"] == 0:
        if (
            integration_request.reference_doctype
            and integration_request.reference_docname
        ):
            try:
                item_response = transaction_response["CallbackMetadata"]["Item"]
                amount = fetch_param_value(item_response, "Amount", "Name")
                mpesa_receipt = fetch_param_value(
                    item_response, "MpesaReceiptNumber", "Name"
                )
                pr = frappe.get_doc(
                    integration_request.reference_doctype,
                    integration_request.reference_docname,
                )

                mpesa_receipts, completed_payments = (
                    get_completed_integration_requests_info(
                        integration_request.reference_doctype,
                        integration_request.reference_docname,
                        checkout_id,
                    )
                )

                total_paid = amount + sum(completed_payments)
                mpesa_receipts = ", ".join(mpesa_receipts + [mpesa_receipt])

                if total_paid >= pr.grand_total:
                    pr.run_method("on_payment_authorized", "Completed")
                    success = True

                frappe.db.set_value(
                    "POS Invoice",
                    pr.reference_name,
                    "mpesa_receipt_number",
                    mpesa_receipts,
                )
                integration_request.handle_success(transaction_response)
            except Exception:
                integration_request.handle_failure(transaction_response)
                frappe.log_error("Mpesa: Failed to verify transaction")

    else:
        integration_request.handle_failure(transaction_response)

    frappe.publish_realtime(
        event="process_phone_payment",
        doctype="POS Invoice",
        docname=transaction_data.payment_reference,
        user=integration_request.owner,
        message={
            "amount": total_paid,
            "success": success,
            "failure_message": (
                transaction_response["ResultDesc"]
                if transaction_response["ResultCode"] != 0
                else ""
            ),
        },
    )


def build_pull_payload(
    mpesa_settings: str, start_date: str, end_date: str, offset: int = 0
) -> dict:
    def _fmt(dt_str: str) -> str:
        return datetime.datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M:%S").strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, mpesa_settings)
    shortcode = (
        settings.till_number if settings.sandbox else settings.business_shortcode
    )

    return {
        "ShortCode": shortcode,
        "StartDate": _fmt(start_date),
        "EndDate": _fmt(end_date),
        "OffSetValue": str(int(offset)),
    }


@frappe.whitelist()
def pull_transactions(
    mpesa_settings: str,
    start_date: str,
    end_date: str,
    offset: int = 0,
) -> dict:
    try:
        payload = build_pull_payload(mpesa_settings, start_date, end_date, offset)

    except Exception:
        frappe.log_error(frappe.get_traceback(), "Mpesa Pull Transaction Error")
        return {
            "status": "error",
            "message": "Failed to pull transactions. Check Error Logs.",
        }

    try:
        frappe.enqueue(
            "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.execute_pull_transactions",
            queue="long",
            timeout=600,
            mpesa_settings=mpesa_settings,
            payload=payload,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Mpesa Pull Transaction Error")
        return {
            "status": "error",
            "message": "Failed to pull transactions. Check Error Logs.",
        }

    return {
        "status": "success",
        "message": "Pull request queued. Importing transactions in the background...",
    }


# Connect timeouts to api.safaricom.co.ke are routine when the hourly job fans
# out one request per Mpesa Settings doc. Retry rather than lose the window.
PULL_MAX_ATTEMPTS = 3
PULL_RETRY_BACKOFF_SECONDS = 2

# Safety stop for the pagination walk, in case TotalPages is missing or wrong.
PULL_MAX_PAGES = 50


def execute_pull_transactions(mpesa_settings: str, payload: dict) -> None:
    """Pull every page Safaricom has for the window, not just the first one.

    Safaricom paginates: a 48h window on a busy shortcode can come back as
    several hundred records across several pages. We used to send OffSetValue once and
    import whatever single page came back, silently dropping the rest unless
    someone manually re-ran with offset 1, 2, 3. Now we walk the pages.

    Runs in the background (enqueued by `pull_transactions`). Connection-level
    failures are retried per page: the hourly job fans out one request per
    Mpesa Settings doc against a single Safaricom host, so transient connect
    timeouts are expected and shouldn't be treated as a lost pull.
    """
    # A multi-page pull would otherwise fire one alert per page. Collect them
    # and report once, unless a bulk run is already collecting.
    outer_bulk = bool(frappe.flags.get(BULK_PULL_FLAG))
    if not outer_bulk:
        frappe.flags[BULK_PULL_FLAG] = True
        frappe.flags[BULK_PULL_RESULTS_FLAG] = []

    results = frappe.flags.get(BULK_PULL_RESULTS_FLAG)
    first_result = len(results or [])

    page = frappe.utils.cint(payload.get("OffSetValue") or 0)
    last_error = None

    try:
        while page < PULL_MAX_PAGES:
            payload["OffSetValue"] = str(page)
            data = _pull_one_page(mpesa_settings, payload)

            if isinstance(data, Exception):
                last_error = data
                break
            if data is None:
                break

            total_pages = frappe.utils.cint(data.get("TotalPages") or 0)
            page += 1
            if page >= total_pages:
                break
    finally:
        if not outer_bulk:
            frappe.flags[BULK_PULL_FLAG] = False

    if last_error is None:
        if not outer_bulk:
            _publish_pull_summary(mpesa_settings, (results or [])[first_result:])
        return

    _handle_pull_failure(mpesa_settings, last_error)


def _pull_one_page(mpesa_settings: str, payload: dict):
    """One page, with retries. Returns the response, None, or the exception."""
    for attempt in range(PULL_MAX_ATTEMPTS):
        try:
            return process_request(
                endpoint="/pulltransactions/v1/query",
                settings_name=mpesa_settings,
                method="POST",
                payload=payload,
                success_callback=pull_transaction_on_success,
                error_callback=pull_transaction_on_error,
                request_description="Mpesa Pull Transaction",
                doctype=MPESA_SETTINGS_DOCTYPE,
                document_name=mpesa_settings,
            )
        except (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ) as e:
            if attempt < PULL_MAX_ATTEMPTS - 1:
                time.sleep(PULL_RETRY_BACKOFF_SECONDS * (2**attempt))
                continue
            return e
        except Exception as e:
            # Not a network blip - don't burn retries on a real bug.
            return e

    return None


def _publish_pull_summary(mpesa_settings: str, page_results: list) -> None:
    """One alert for the whole pull, however many pages it took."""
    if not page_results:
        return

    if len(page_results) == 1:
        publish_pull_result(page_results[0])
        return

    created = sum(r.get("created") or 0 for r in page_results)
    skipped = sum(r.get("skipped") or 0 for r in page_results)
    failed = sum(r.get("failed") or 0 for r in page_results)

    publish_pull_result(
        {
            "status": "success" if created or skipped else "warning",
            "title": "Pull Transaction Complete",
            "message": (
                f"{mpesa_settings}: {created} record(s) imported, {skipped} skipped, "
                f"{failed} failed across {len(page_results)} page(s)."
            ),
            "count": created,
            "settings": mpesa_settings,
            "created": created,
            "skipped": skipped,
            "failed": failed,
        }
    )


def _handle_pull_failure(mpesa_settings: str, error) -> None:
    is_network = isinstance(
        error,
        (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ),
    )

    if is_network:
        frappe.log_error(
            f"Gave up after {PULL_MAX_ATTEMPTS} attempts: {error}",
            f"Mpesa Pull Transaction Error [{mpesa_settings}]",
        )
        detail = f"connection failed after {PULL_MAX_ATTEMPTS} attempts"
    else:
        frappe.log_error(str(error), f"Mpesa Pull Transaction Error [{mpesa_settings}]")
        detail = "Check Error Logs"

    record_pull_outcome(mpesa_settings, status="Error", message=str(error or detail))

    publish_pull_result(
        {
            "status": "error",
            "title": "Pull Transaction Failed",
            "message": f"{mpesa_settings}: Failed to pull transactions - {detail}.",
            "count": 0,
            "settings": mpesa_settings,
        }
    )


@frappe.whitelist()
def bulk_pull_transactions(
    settings_names: str | list | None = None,
    start_date: str = "",
    end_date: str = "",
    offset: int = 0,
) -> dict:
    """Queue one pull covering many shortcodes, reporting a single summary."""
    frappe.only_for(["System Manager", "Administrator"])

    if isinstance(settings_names, str):
        settings_names = frappe.parse_json(settings_names)

    if not settings_names:
        settings_names = [
            row.name
            for row in frappe.get_all(
                MPESA_SETTINGS_DOCTYPE,
                filters={"enable_hourly_pull_transactions": 1},
                fields=["name"],
            )
        ]

    if not settings_names:
        return {"status": "error", "message": _("No Mpesa Settings selected.")}

    if not start_date or not end_date:
        return {"status": "error", "message": _("Start and end date are required.")}

    frappe.enqueue(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api"
        ".execute_bulk_pull_transactions",
        queue="long",
        timeout=3600,
        settings_names=settings_names,
        start_date=start_date,
        end_date=end_date,
        offset=offset,
    )

    return {
        "status": "success",
        "message": _(
            "Pull queued for {0} shortcode(s). One summary will follow."
        ).format(len(settings_names)),
        "count": len(settings_names),
    }


def execute_bulk_pull_transactions(
    settings_names: list, start_date: str, end_date: str, offset: int = 0
) -> None:
    """Pull each shortcode in turn, collecting results into one summary.

    Runs the pulls inline rather than enqueuing one job each, so the per-pull
    toasts can be suppressed via BULK_PULL_FLAG and replaced by a single message.
    """
    frappe.flags[BULK_PULL_FLAG] = True
    frappe.flags[BULK_PULL_BATCH_FLAG] = True
    frappe.flags[BULK_PULL_RESULTS_FLAG] = []

    skipped_settings = []

    try:
        for name in settings_names:
            try:
                payload = build_pull_payload(name, start_date, end_date, offset)
            except Exception as e:
                skipped_settings.append(f"{name}: {e}")
                continue

            try:
                execute_pull_transactions(mpesa_settings=name, payload=payload)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Mpesa Bulk Pull Error [{name}]",
                )
            frappe.db.commit()

        results = frappe.flags.get(BULK_PULL_RESULTS_FLAG) or []
    finally:
        frappe.flags[BULK_PULL_FLAG] = False
        frappe.flags[BULK_PULL_BATCH_FLAG] = False

    imported = sum(r.get("created") or 0 for r in results)
    no_data = [r for r in results if r.get("response_code") == "1001"]
    errored = [r for r in results if r.get("status") == "error"]

    summary_lines = [
        f"Window: {start_date} -> {end_date}",
        f"Shortcodes attempted: {len(settings_names)}",
        f"Records imported: {imported}",
        f"Returned no data (1001): {len(no_data)}",
        f"Errored: {len(errored)}",
        "",
        "No data: " + ", ".join(str(r.get("settings")) for r in no_data),
        "",
        "Errors:",
    ]
    summary_lines += [f"  {r.get('message')}" for r in errored]
    if skipped_settings:
        summary_lines += ["", "Skipped (bad config):"] + [
            f"  {s}" for s in skipped_settings
        ]

    frappe.log_error(
        title="Mpesa Bulk Pull Transactions Complete",
        message="\n".join(summary_lines),
    )

    frappe.publish_realtime(
        event="mpesa_bulk_pull_complete",
        message={
            "status": "success" if not errored else "warning",
            "title": "Bulk Pull Complete",
            "message": (
                f"{imported} record(s) imported across {len(settings_names)} shortcode(s). "
                f"{len(no_data)} returned no data, {len(errored)} errored."
            ),
        },
        user=frappe.session.user,
    )
