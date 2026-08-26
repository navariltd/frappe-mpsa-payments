import frappe
from frappe.utils import now_datetime

from ...utils.doctype_names import (
    MPESA_C2B_PAYMENT_REGISTER_DOCTYPE,
    MPESA_EXPRESS_REQUEST_DOCTYPE,
    MPESA_SETTINGS_DOCTYPE,
)
from ...utils.utils import (
    log_and_throw_error,
    update_mpesa_request_status,
)


def balance_query_on_success(response: dict, document_name: str, **kwargs) -> None:
    pass


def transaction_status_on_success(response: dict, document_name: str, **kwargs) -> None:
    try:
        # frappe.set_user("Administrator")

        frappe.flags.ignore_permissions = True

        result_code = response.get("ResultCode")
        status = "Completed" if result_code == "0" else "Failed"

        request_doc = frappe.get_doc(MPESA_EXPRESS_REQUEST_DOCTYPE, document_name)

        update_mpesa_request_status(
            document_name,
            {
                "result_code": result_code,
                "result_desc": response.get("ResultDesc"),
                "merchant_request_id": response.get("MerchantRequestID"),
                "checkout_request_id": response.get("CheckoutRequestID"),
                "response_code": response.get("ResponseCode"),
                "response_description": response.get("ResponseDescription"),
                "status": status,
            },
        )
        request_doc.reconcile_payment()

    except Exception:
        log_and_throw_error("MPESA Transaction Status Update Error", document_name)


def stk_push_on_success(
    response: dict, payload: dict, document_name: str, **kwargs
) -> None:
    try:
        fields = {
            "merchant_request_id": response.get("MerchantRequestID", ""),
            "checkout_request_id": response.get("CheckoutRequestID", ""),
            "response_code": response.get("ResponseCode", ""),
            "response_description": response.get("ResponseDescription", ""),
            "customer_message": response.get("CustomerMessage", ""),
            "amount": payload.get("Amount", 0.0),
            "phone_number": payload.get("PhoneNumber", ""),
            "timestamp": now_datetime(),
            "settings": kwargs.get("settings_name", ""),
        }

        doctype = kwargs.get("doctype", "")

        if doctype == MPESA_EXPRESS_REQUEST_DOCTYPE:
            for key, value in fields.items():
                frappe.db.set_value(
                    MPESA_EXPRESS_REQUEST_DOCTYPE, document_name, key, value
                )
            frappe.logger().info(f"Mpesa Express Request updated for {document_name}")
        else:
            doc = frappe.new_doc(MPESA_EXPRESS_REQUEST_DOCTYPE)
            for key, value in fields.items():
                setattr(doc, key, value)
            doc.insert(ignore_permissions=True)
            frappe.logger().info(
                f"Mpesa Express Request created for {document_name} with ID {doc.name}"
            )

        frappe.db.commit()

        frappe.publish_realtime(
            event="refresh_form",
            doctype=MPESA_EXPRESS_REQUEST_DOCTYPE,
            docname=document_name,
        )

        # frappe.enqueue(
        #     "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.check_transaction_status",
        #     name=document_name,
        #     enqueue_after_commit=True,
        #     timeout=300
        # )

    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"STK Push Success Error for {document_name}"
        )
        raise


def stk_push_on_error(
    response: dict, payload: dict, document_name: str, **kwargs
) -> None:
    try:
        frappe.db.set_value(
            MPESA_EXPRESS_REQUEST_DOCTYPE,
            document_name,
            {
                # "response_code": response.get("errorCode", ""),
                "response_description": response.get("errorMessage", ""),
                "status": "Failed",
            },
        )
        frappe.db.commit()

    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"STK Push Success Error for {document_name}"
        )
        raise


PULL_SUCCESS_CODE = "1000"

# Safaricom returns this with HTTP 200 both when a window genuinely has no
# transactions and when the shortcode is not provisioned for Pull at all
# (the "or Organization Name not available" half of the message). Treating it
# as a plain success is what let ~4,600 failed pulls report "0 imported".
PULL_NO_RECORDS_CODE = "1001"

PULL_NO_RECORDS_HINT = (
    "Safaricom returns 1001 both for an empty window and for a shortcode that is "
    "not provisioned for Pull. If this repeats for a shortcode that is known to be "
    "receiving payments, the registration or Organization Name is likely missing "
    "on Safaricom's side."
)


BULK_PULL_FLAG = "mpesa_bulk_pull"
BULK_PULL_RESULTS_FLAG = "mpesa_bulk_pull_results"

# Set only when sweeping many shortcodes at once (hourly job, bulk pull action).
# In that mode a per-shortcode 1001 is noise: most branches simply took no
# payments in the window, and the run writes one summary listing all of them.
BULK_PULL_BATCH_FLAG = "mpesa_bulk_pull_batch"


def publish_pull_result(message: dict) -> None:
    """Emit one toast per pull - unless a bulk run is collecting them.

    A bulk pull over many shortcodes would otherwise fire one popup each. The
    bulk worker sets the flag, runs everything in its own job, and publishes a
    single summary at the end.
    """
    if frappe.flags.get(BULK_PULL_FLAG):
        results = frappe.flags.get(BULK_PULL_RESULTS_FLAG)
        if results is None:
            results = []
            frappe.flags[BULK_PULL_RESULTS_FLAG] = results
        results.append(message)
        return

    frappe.publish_realtime(
        event="mpesa_pull_transaction_complete",
        message=message,
        user=frappe.session.user,
    )


def record_pull_outcome(
    settings_name: str,
    status: str,
    response_code: str = "",
    message: str = "",
) -> None:
    """Persist the last pull outcome on Mpesa Settings.

    Without this, a shortcode that never returns data is indistinguishable from
    one that simply had a quiet hour. Written with db_set on a fresh doc load so
    it survives regardless of what the caller does with its own copy.
    """
    try:
        frappe.db.set_value(
            MPESA_SETTINGS_DOCTYPE,
            settings_name,
            {
                "last_pull_status": status,
                "last_pull_response_code": response_code or "",
                "last_pull_message": (message or "")[:500],
                "last_pull_on": now_datetime(),
            },
            update_modified=False,
        )
    except Exception:
        # Never let bookkeeping break the pull itself.
        frappe.log_error(
            frappe.get_traceback(),
            f"Mpesa Pull Transaction: could not record outcome for {settings_name}",
        )


def _flatten_pull_transactions(raw) -> list:
    if isinstance(raw, dict):
        return [raw]
    if not isinstance(raw, list):
        return []
    flat = []
    for item in raw:
        if isinstance(item, list):
            flat.extend(_flatten_pull_transactions(item))
        elif isinstance(item, dict):
            flat.append(item)
    return flat


def pull_transaction_on_success(response: dict, document_name: str, **kwargs) -> None:
    settings = frappe.get_doc(
        MPESA_SETTINGS_DOCTYPE, kwargs.get("settings_name", document_name)
    )
    shortcode = (
        settings.till_number if settings.sandbox else settings.business_shortcode
    )

    # This callback runs on any HTTP 2xx, but Safaricom signals application-level
    # failure in the body while still returning 200. Anything other than 1000 means
    # no data was returned and must not be reported as a successful import.
    response_code = str(response.get("ResponseCode") or "")
    if response_code and response_code != PULL_SUCCESS_CODE:
        response_message = str(
            response.get("ResponseMessage") or response.get("errorMessage") or ""
        )
        is_no_records = response_code == PULL_NO_RECORDS_CODE

        log_message = [
            f"Settings: {settings.name}",
            f"ShortCode: {shortcode!r}",
            f"ResponseCode: {response_code}",
            f"ResponseMessage: {response_message}",
            f"Window: {kwargs.get('payload', {})}",
        ]
        if is_no_records:
            log_message.append("")
            log_message.append(PULL_NO_RECORDS_HINT)

        # In a batch sweep the summary covers this; logging each shortcode
        # separately produced ~800 near-identical entries a day.
        if not frappe.flags.get(BULK_PULL_BATCH_FLAG):
            frappe.log_error(
                title=f"Mpesa Pull Transaction: {response_code} from Safaricom",
                message="\n".join(log_message),
            )

        record_pull_outcome(
            settings.name,
            status="No Data" if is_no_records else "Error",
            response_code=response_code,
            message=response_message,
        )

        publish_pull_result(
            {
                "status": "warning" if is_no_records else "error",
                "title": "Pull Transaction Returned No Data",
                "message": (
                    f"{settings.name}: Safaricom returned {response_code} - "
                    f"{response_message or 'no message'}. Nothing imported."
                ),
                "count": 0,
                "response_code": response_code,
                "settings": settings.name,
            }
        )
        return

    # Safaricom's actual response nests one list of transaction dicts inside
    # "Response" (confirmed via live sandbox test) - not the "Transactions.Transaction"
    # shape assumed at plan time.
    transactions = _flatten_pull_transactions(response.get("Response", []))

    created, skipped, failed = 0, 0, 0
    created_records = []
    duplicate_transids = []
    malformed = []

    for txn in transactions:
        try:
            transid = txn.get("transactionId", "")
            if not transid:
                malformed.append(str(txn))
                skipped += 1
                continue

            if frappe.db.exists(
                MPESA_C2B_PAYMENT_REGISTER_DOCTYPE, {"transid": transid}
            ):
                duplicate_transids.append(transid)
                skipped += 1
                continue

            doc = frappe.new_doc(MPESA_C2B_PAYMENT_REGISTER_DOCTYPE)
            doc.transid = transid
            doc.transtime = txn.get("trxDate", "")
            doc.transamount = float(txn.get("amount") or 0.0)
            doc.msisdn = txn.get("msisdn", "")
            doc.businessshortcode = shortcode
            doc.billrefnumber = txn.get("billreference", "")
            doc.transactiontype = txn.get("transactiontype", "")
            doc.insert(ignore_permissions=True)

            created_records.append(
                {
                    "transid": transid,
                    "doc": doc.name,
                    "amount": doc.transamount,
                    "msisdn": doc.msisdn,
                }
            )
            created += 1

        except Exception:
            failed += 1
            frappe.log_error(
                frappe.get_traceback(),
                f"Mpesa Pull Transaction: Failed for transid={txn.get('transactionId', '')}",
            )
            continue

    frappe.db.commit()

    # One log per pull, not one per transaction. A busy shortcode re-pulls
    # dozens of transactions the webhook already delivered, and logging each
    # duplicate separately buried everything else.
    if created_records or duplicate_transids or malformed:
        summary = [
            f"Settings: {settings.name}",
            f"ShortCode: {shortcode!r}",
            f"Window: {kwargs.get('payload', {})}",
            f"Returned by Safaricom: {len(transactions)}",
            f"Created: {created}   Skipped: {skipped}   Failed: {failed}",
        ]
        if created_records:
            summary += ["", "Created:", frappe.as_json(created_records)]
        if duplicate_transids:
            summary += [
                "",
                f"Already present, skipped ({len(duplicate_transids)}):",
                ", ".join(duplicate_transids),
            ]
        if malformed:
            summary += ["", "Missing transactionId:"] + malformed

        frappe.log_error(
            title=f"Mpesa Pull Transaction: {settings.name} - {created} created, {skipped} skipped",
            message="\n".join(summary),
        )

    record_pull_outcome(
        settings.name,
        status="Success",
        response_code=response_code or PULL_SUCCESS_CODE,
        message=f"{created} imported, {skipped} skipped, {failed} failed "
        f"from {len(transactions)} returned",
    )

    publish_pull_result(
        {
            "status": "success"
            if created > 0 or skipped == len(transactions)
            else "warning",
            "title": "Pull Transaction Complete",
            "message": f"{settings.name}: {created} record(s) imported, {skipped} skipped, {failed} failed.",
            "count": created,
            "response_code": response_code or PULL_SUCCESS_CODE,
            "settings": settings.name,
            "created": created,
            "skipped": skipped,
            "failed": failed,
        }
    )


def pull_transaction_on_error(response: dict, document_name: str, **kwargs) -> None:
    settings_name = kwargs.get("settings_name") or document_name
    frappe.log_error(
        title="Mpesa Pull Transaction: Safaricom Error",
        message=frappe.as_json(response),
    )
    error_message = response.get(
        "errorMessage", "Safaricom rejected the pull request. Check Error Logs."
    )
    record_pull_outcome(
        settings_name,
        status="Error",
        response_code=str(
            response.get("errorCode") or response.get("ResponseCode") or ""
        ),
        message=str(error_message),
    )
    publish_pull_result(
        {
            "status": "error",
            "title": "Pull Transaction Failed",
            "message": f"{settings_name}: {error_message}",
            "count": 0,
            "settings": settings_name,
        }
    )
