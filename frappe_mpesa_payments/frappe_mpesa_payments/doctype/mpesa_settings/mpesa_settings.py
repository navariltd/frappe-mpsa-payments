# Copyright (c) 2020, Frappe Technologies and contributors
# For license information, please see license.txt


import base64
from json import dumps, loads
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate

import frappe
from frappe import _, get_single
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, fmt_money, get_request_site_address
from frappe.utils.file_manager import get_file_path

from ....utils.doctype_names import PUBLIC_CERTIFICATES_DOCTYPE
from ....utils.utils import erpnext_app_import_guard
from .mpesa_connector import MpesaConnector
from .mpesa_custom_fields import create_custom_pos_fields


class MpesaSettings(Document):
    supported_currencies = ["KES"]

    def validate_transaction_currency(self, currency: str) -> None:
        if currency not in self.supported_currencies:
            frappe.throw(
                _(
                    "Please select another payment method. Mpesa does not support transactions in currency '{0}'"
                ).format(currency)
            )

    def before_insert(self) -> None:
        """Before Insertion hook"""
        if self.api_type == "MPesa B2C (Business to Customer)":
            certificate_file = get_single(PUBLIC_CERTIFICATES_DOCTYPE)

            file_path = get_file_path(
                certificate_file.sandbox_certificate
                if self.sandbox
                else certificate_file.production_certificate
            )

            with open(file_path, "rb") as cert_file:
                public_key = load_pem_x509_certificate(
                    cert_file.read(), backend=default_backend()
                ).public_key()

            ciphertext = public_key.encrypt(
                self.online_passkey.encode("utf-8"),
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )

            self.security_credential = base64.b64encode(ciphertext).decode("utf-8")

    def on_update(self) -> None:
        """On Update Hook"""
        from ....utils.utils import create_payment_gateway

        if "erpnext" in frappe.get_installed_apps():
            create_custom_pos_fields()

        create_payment_gateway(
            "Mpesa-" + self.payment_gateway_name,
            settings="Mpesa Settings",
            controller=self.payment_gateway_name,
        )
        call_hook_method(
            "payment_gateway_enabled",
            gateway="Mpesa-" + self.payment_gateway_name,
            payment_channel="Phone",
        )

        # required to fetch the bank account details from the payment gateway account
        frappe.db.commit()  # nosemgrep
        create_mode_of_payment(
            "Mpesa-" + self.payment_gateway_name, payment_type="Phone"
        )

    def request_for_payment(self, **kwargs) -> None:
        args = frappe._dict(kwargs)
        request_amounts = self.split_request_amount_according_to_transaction_limit(args)

        for i, amount in enumerate(request_amounts):
            args.request_amount = amount
            if frappe.flags.in_test:
                from .test_mpesa_settings import get_payment_request_response_payload

                response = frappe._dict(get_payment_request_response_payload(amount))
            else:
                response = frappe._dict(generate_stk_push(**args))

            self.handle_api_response("CheckoutRequestID", args, response)

    def split_request_amount_according_to_transaction_limit(
        self, args: frappe._dict
    ) -> list:
        request_amount = args.request_amount
        if request_amount > self.transaction_limit:
            # make multiple requests
            request_amounts = []
            requests_to_be_made = frappe.utils.ceil(
                request_amount / self.transaction_limit
            )  # 480/150 = ceil(3.2) = 4
            for i in range(requests_to_be_made):
                amount = self.transaction_limit
                if i == requests_to_be_made - 1:
                    amount = request_amount - (
                        self.transaction_limit * i
                    )  # for 4th request, 480 - (150 * 3) = 30
                request_amounts.append(amount)
        else:
            request_amounts = [request_amount]

        return request_amounts

    @frappe.whitelist()
    def get_account_balance_info(self) -> None:
        payload = dict(
            reference_doctype="Mpesa Settings",
            reference_docname=self.name,
            doc_details=vars(self),
        )

        if frappe.flags.in_test:
            from .test_mpesa_settings import get_test_account_balance_response

            response = frappe._dict(get_test_account_balance_response())
        else:
            response = frappe._dict(get_account_balance(payload))

        self.handle_api_response("ConversationID", payload, response)

    def handle_api_response(
        self, global_id: str, request_dict: frappe._dict, response: frappe._dict
    ) -> None:
        """Response received from API calls returns a global identifier for each transaction, this code is returned during the callback."""
        # check error response
        if response["requestId"]:
            req_name = response["requestId"]
            error = response
        else:
            # global checkout id used as request name
            req_name = response[global_id]
            error = None

        if not frappe.db.exists("Integration Request", req_name):
            create_request_log(request_dict, "Host", "Mpesa", req_name, error)

        if error:
            frappe.throw(_(response["errorMessage"]), title=_("Transaction Error"))


def generate_stk_push(**kwargs) -> str | Any:
    """Generate stk push by making a API call to the stk push API."""
    args = frappe._dict(kwargs)

    try:
        callback_url = (
            get_request_site_address(True)
            + "/api/method/payments.payment_gateways.doctype.mpesa_settings.mpesa_settings.verify_transaction"
        )

        mpesa_settings = frappe.get_doc("Mpesa Settings", args.payment_gateway[6:])
        env = "production" if not mpesa_settings.sandbox else "sandbox"
        # for sandbox, business shortcode is same as till number
        business_shortcode = (
            mpesa_settings.business_shortcode
            if env == "production"
            else mpesa_settings.till_number
        )

        connector = MpesaConnector(
            env=env,
            app_key=mpesa_settings.consumer_key,
            app_secret=mpesa_settings.get_password("consumer_secret"),
        )

        mobile_number = sanitize_mobile_number(args.sender)

        response = connector.stk_push(
            business_shortcode=business_shortcode,
            amount=args.request_amount,
            passcode=mpesa_settings.get_password("online_passkey"),
            callback_url=callback_url,
            reference_code=mpesa_settings.till_number,
            phone_number=mobile_number,
            description="POS Payment",
        )

        return response

    except Exception:
        frappe.log_error("Mpesa Express Transaction Error")
        frappe.throw(
            _(
                "Issue detected with Mpesa configuration, check the error logs for more details"
            ),
            title=_("Mpesa Express Error"),
        )


def sanitize_mobile_number(number: str) -> str:
    """Add country code and strip leading zeroes from the phone number."""
    return "254" + str(number).lstrip("0")


@frappe.whitelist(allow_guest=True)
def verify_transaction(**kwargs) -> None:
    """Verify the transaction result received via callback from stk."""
    transaction_response = frappe._dict(kwargs["Body"]["stkCallback"])

    checkout_id = getattr(transaction_response, "CheckoutRequestID", "")
    if not isinstance(checkout_id, str):
        frappe.throw(_("Invalid Checkout Request ID"))

    integration_request = frappe.get_doc("Integration Request", checkout_id)
    transaction_data = frappe._dict(loads(integration_request.data))
    total_paid = 0  # for multiple integration request made against a pos invoice
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


def get_completed_integration_requests_info(
    reference_doctype: str, reference_docname: str, checkout_id: str
) -> tuple[list, list]:
    output_of_other_completed_requests = frappe.get_all(
        "Integration Request",
        filters={
            "name": ["!=", checkout_id],
            "reference_doctype": reference_doctype,
            "reference_docname": reference_docname,
            "status": "Completed",
        },
        pluck="output",
    )

    mpesa_receipts, completed_payments = [], []

    for out in output_of_other_completed_requests:
        out = frappe._dict(loads(out))
        item_response = out["CallbackMetadata"]["Item"]
        completed_amount = fetch_param_value(item_response, "Amount", "Name")
        completed_mpesa_receipt = fetch_param_value(
            item_response, "MpesaReceiptNumber", "Name"
        )
        completed_payments.append(completed_amount)
        mpesa_receipts.append(completed_mpesa_receipt)

    return mpesa_receipts, completed_payments


def get_account_balance(request_payload: dict) -> str | dict | None:
    """Call account balance API to send the request to the Mpesa Servers."""
    try:
        mpesa_settings = frappe.get_doc(
            "Mpesa Settings", request_payload.get("reference_docname")
        )
        env = "production" if not mpesa_settings.sandbox else "sandbox"
        connector = MpesaConnector(
            env=env,
            app_key=mpesa_settings.consumer_key,
            app_secret=mpesa_settings.get_password("consumer_secret"),
        )

        callback_url = (
            get_request_site_address(True)
            + "/api/method/payments.payment_gateways.doctype.mpesa_settings.mpesa_settings.process_balance_info"
        )

        response = connector.get_balance(
            mpesa_settings.initiator_name,
            mpesa_settings.security_credential,
            mpesa_settings.till_number,
            4,
            mpesa_settings.name,
            callback_url,
            callback_url,
        )
        return response
    except Exception:
        frappe.log_error("Mpesa: Failed to get account balance")
        frappe.throw(
            _("Please check your configuration and try again"), title=_("Error")
        )


@frappe.whitelist(allow_guest=True)
def process_balance_info(**kwargs) -> None:
    """Process and store account balance information received via callback from the account balance API call."""
    account_balance_response = frappe._dict(kwargs["Result"])

    conversation_id = getattr(account_balance_response, "ConversationID", "")
    if not isinstance(conversation_id, str):
        frappe.throw(_("Invalid Conversation ID"))

    request = frappe.get_doc("Integration Request", conversation_id)

    if request.status == "Completed":
        return

    transaction_data = frappe._dict(loads(request.data))

    if account_balance_response["ResultCode"] == 0:
        try:
            result_params = account_balance_response["ResultParameters"][
                "ResultParameter"
            ]

            balance_info = fetch_param_value(result_params, "AccountBalance", "Key")
            balance_info = format_string_to_json(balance_info)

            ref_doc = frappe.get_doc(
                transaction_data.reference_doctype, transaction_data.reference_docname
            )
            ref_doc.db_set("account_balance", balance_info)

            request.handle_success(account_balance_response)
            frappe.publish_realtime(
                "refresh_mpesa_dashboard",
                doctype="Mpesa Settings",
                docname=transaction_data.reference_docname,
                user=transaction_data.owner,
            )
        except Exception:
            request.handle_failure(account_balance_response)
            frappe.log_error(
                title="Mpesa Account Balance Processing Error",
                message=account_balance_response,
            )
    else:
        request.handle_failure(account_balance_response)


def format_string_to_json(balance_info: str) -> str:
    """
    Format string to json.

    e.g: '''Working Account|KES|481000.00|481000.00|0.00|0.00'''
    => {'Working Account': {'current_balance': '481000.00',
            'available_balance': '481000.00',
            'reserved_balance': '0.00',
            'uncleared_balance': '0.00'}}
    """
    balance_dict = frappe._dict()
    for account_info in balance_info.split("&"):
        account_info = account_info.split("|")
        balance_dict[account_info[0]] = dict(
            current_balance=fmt_money(account_info[2], currency="KES"),
            available_balance=fmt_money(account_info[3], currency="KES"),
            reserved_balance=fmt_money(account_info[4], currency="KES"),
            uncleared_balance=fmt_money(account_info[5], currency="KES"),
        )
    return dumps(balance_dict)


def fetch_param_value(response: dict, key: str, key_field: str) -> str | None:
    """Fetch the specified key from list of dictionary. Key is identified via the key field."""
    for param in response:
        if param[key_field] == key:
            return param["Value"]


def create_mode_of_payment(gateway: str, payment_type: str = "General") -> Document:
    with erpnext_app_import_guard():
        from erpnext import get_default_company

    payment_gateway_account = frappe.db.get_value(
        "Payment Gateway Account", {"payment_gateway": gateway}, ["payment_account"]
    )

    mode_of_payment = frappe.db.exists("Mode of Payment", gateway)
    if not mode_of_payment and payment_gateway_account:
        mode_of_payment = frappe.get_doc(
            {
                "doctype": "Mode of Payment",
                "mode_of_payment": gateway,
                "enabled": 1,
                "type": payment_type,
                "accounts": [
                    {
                        "doctype": "Mode of Payment Account",
                        "company": get_default_company(),
                        "default_account": payment_gateway_account,
                    }
                ],
            }
        )
        mode_of_payment.insert(ignore_permissions=True)

        return mode_of_payment

    return frappe.get_doc("Mode of Payment", mode_of_payment)
