# Copyright (c) 2020, Frappe Technologies and contributors
# For license information, please see license.txt


import base64
import re
import time
from json import dumps, loads
from typing import Any
from urllib.parse import urlparse

import frappe
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate
from frappe import _, get_single
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import (
    fmt_money,
    get_request_site_address,
    now_datetime,
)
from frappe.utils.file_manager import get_file_path

from frappe_mpsa_payments.utils.encoding_initiator_password import (
    generate_security_credential,
)

from ....utils.doctype_names import (
    MPESA_EXPRESS_REQUEST_DOCTYPE,
    PUBLIC_CERTIFICATES_DOCTYPE,
)
from ....utils.utils import (
    create_payment_gateway_account,
    erpnext_app_import_guard,
    validate_phone_number,
)
from ...api.m_pesa_api import get_account_balance
from .mpesa_connector import MpesaConnector


class MpesaSettings(Document):
    supported_currencies = ["KES"]

    def validate_transaction_currency(self, currency: str) -> None:
        """
        Validates that the transaction currency is supported for Mpesa.

        Allows the transaction if:
        - The currency is KES, OR
        - The company's default currency is KES
        """
        if currency in self.supported_currencies:
            return

        # If company is provided, check if its default currency is KES
        if self.company:
            default_currency = frappe.db.get_value(
                "Company", self.company, "default_currency"
            )
            if default_currency in self.supported_currencies:
                return

        # If neither the currency nor company default is supported
        frappe.throw(
            _(
                "Please select another payment method. Mpesa does not support transactions in currency '{0}'."
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

    @frappe.whitelist()
    def get_payment_url(self, **kwargs) -> str:
        """Return the payment URL"""
        return "/all-products"

    def on_update(self) -> None:
        """On Update Hook"""
        from ....utils.utils import create_payment_gateway

        create_payment_gateway(
            "Mpesa-" + self.payment_gateway_name,
            settings="Mpesa Settings",
            controller=self.payment_gateway_name,
        )

        # erpnext create_payment_gateway_account doesn't allow for company to be passed, ovveriden
        # call_hook_method(
        #     "payment_gateway_enabled",
        #     gateway="Mpesa-" + self.payment_gateway_name,
        #     payment_channel="Phone",
        # )
        create_payment_gateway_account(
            gateway="Mpesa-" + self.payment_gateway_name,
            payment_channel="Phone",
            company=self.company,
        )

        # required to fetch the bank account details from the payment gateway account
        frappe.db.commit()  # nosemgrep
        create_mode_of_payment(
            "Mpesa-" + self.payment_gateway_name,
            payment_type="Phone",
            company=self.company,
        )

    def validate(self) -> None:
        if self.initiator_password and not self.security_credential:
            certs = frappe.get_single("Mpesa Public Key Certificate")
            cert_url = ""
            if self.sandbox:
                cert_url = certs.sandbox_certificate
            else:
                cert_url = certs.production_certificate

            self.security_credential = generate_security_credential(
                self.get_password("initiator_password", "")
                if self.initiator_password
                else "",
                cert_url,
            )

    def request_for_payment(self, **kwargs) -> None:
        args = frappe._dict(kwargs)
        request_amounts = self.split_request_amount_according_to_transaction_limit(args)
        phone_number = args.get("phone_number")

        if not validate_phone_number(phone_number):
            sender = args.get("sender", "")
            if (
                isinstance(sender, str)
                and sender
                and (sender.startswith(("0", "254", "+", "7", "1")))
            ):
                phone_number = sender
        if not phone_number:
            frappe.throw(_("A valid phone number is required for Mpesa payment."))
        else:
            phone_number = sanitize_mobile_number(phone_number)

        for i, amount in enumerate(request_amounts):
            args.request_amount = amount
            if frappe.flags.in_test:
                from .test_mpesa_settings import get_payment_request_response_payload

                _response = frappe._dict(get_payment_request_response_payload(amount))
            else:
                stk_request = frappe.new_doc(MPESA_EXPRESS_REQUEST_DOCTYPE)
                stk_request.update(
                    {
                        "amount": args.get("request_amount", 0.0),
                        "phone_number": phone_number,
                        "timestamp": frappe.utils.now(),
                        "settings": args.payment_gateway[6:],
                        "payment_gateway": args.get("payment_gateway"),
                        "reference_doctype": args.get("reference_doctype"),
                        "reference_name": args.get("reference_docname"),
                    }
                )
                stk_request.flags.ignore_permissions = True
                stk_request.insert(ignore_permissions=True)
                stk_request.submit()

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
        if frappe.flags.in_test:
            from .test_mpesa_settings import get_test_account_balance_response

            frappe._dict(get_test_account_balance_response())
        else:
            get_account_balance(self.name)

    def handle_api_response(
        self, global_id: str, request_dict: frappe._dict, response: frappe._dict
    ) -> None:
        """Response received from API calls returns a global identifier for each transaction, this code is returned during the callback."""
        # check error response
        if "requestId" in response:
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
            + "/api/method/frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.verify_transaction"
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
        mobile_number = sanitize_mobile_number(
            args.phone_number if args.phone_number else args.sender
        )

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


def create_mode_of_payment(
    gateway: str, payment_type: str = "General", company: str = None
) -> Document:
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
                        "company": company or get_default_company(),
                        "default_account": payment_gateway_account,
                    }
                ],
            }
        )
        mode_of_payment.insert(ignore_permissions=True)

        return mode_of_payment

    if not mode_of_payment:
        # No gateway account to attach it to, usually because there was no
        # company to create one in. Looking it up anyway raised "Mode of
        # Payment None not found" and failed the whole settings save.
        return None

    return frappe.get_doc("Mode of Payment", mode_of_payment)


@frappe.whitelist()
def trigger_transaction_status(mpesa_settings, transaction_id, remarks="OK"):
    try:
        site_address = get_request_site_address(True)
        parsed_url = urlparse(site_address)
        site_url = f"{parsed_url.scheme}://{parsed_url.hostname}"

        queue_timeout_url = (
            site_url
            + "/api/method/frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.handle_queue_timeout"
        )
        result_url = (
            site_url
            + "/api/method/frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.handle_transaction_status_result"
        )

        settings = frappe.get_doc("Mpesa Settings", mpesa_settings)

        integration_request = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "is_remote_request": 1,
                "integration_request_service": "Mpesa Transaction Status",
                "reference_doctype": "Mpesa C2B Payment Register",
                "status": "Queued",
                "data": dumps(
                    {
                        "mpesa_settings": mpesa_settings,
                        "transaction_id": transaction_id,
                        "remarks": remarks,
                        "queue_timeout_url": queue_timeout_url,
                        "result_url": result_url,
                    }
                ),
                "method": "POST",
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()

        connector = MpesaConnector(
            env="production" if not settings.sandbox else "sandbox",
            app_key=settings.consumer_key,
            app_secret=settings.get_password("consumer_secret"),
        )

        response = connector.transaction_status(
            initiator=settings.initiator_name,
            security_credential=settings.security_credential,
            transaction_id=transaction_id,
            party_a=settings.business_shortcode
            if not settings.sandbox
            else settings.till_number,
            identifier_type=4,  # Organization Short Code
            remarks=remarks,
            occasion="",
            queue_timeout_url=queue_timeout_url,
            result_url=result_url,
        )

        status = "Completed" if response.get("ResponseCode") == "0" else "Failed"
        output = (
            frappe.as_json(response)
            if status == "Completed"
            else f"{response.get('errorCode', 'Unknown')}: {response.get('errorMessage', 'Unknown error')}"
        )

        frappe.db.set_value(
            "Integration Request",
            integration_request.name,
            {
                "status": status,
                "output": output,
                "request_id": response.get("OriginatorConversationID"),
            },
            update_modified=True,
        )

        frappe.db.commit()  # nosegrep

        if status == "Completed":
            return {
                "status": "success",
                "message": response.get(
                    "ResponseDescription", "Transaction status check completed"
                ),
                "data": response,
            }

        else:
            return {"status": "error", "message": output}

    except Exception as e:
        if "integration_request" in locals():
            frappe.db.set_value(
                "Integration Request",
                integration_request.name,
                {"status": "Failed", "output": str(e)},
                update_modified=True,
            )
            frappe.db.commit()  # nosegrep

        frappe.log_error(title="Mpesa Transaction Status Error", message=str(e))
        return {"status": "error", "message": str(e)}


# Safaricom throttles bursts; pace the batch rather than fire 69 requests flat out.
BULK_REGISTRATION_DELAY_SECONDS = 1

PULL_REGISTRATION_CALLBACK = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype"
    ".mpesa_settings.mpesa_settings.pull_transaction_callback"
)


def _record_registration_outcome(settings_name: str, status: str, message: str) -> None:
    """Persist pull registration state so enrollment is visible in the list view."""
    try:
        frappe.db.set_value(
            "Mpesa Settings",
            settings_name,
            {
                "pull_registration_status": status,
                "pull_registration_message": (message or "")[:500],
                "pull_registered_on": now_datetime(),
            },
            update_modified=False,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Mpesa Pull Registration: could not record outcome for {settings_name}",
        )


@frappe.whitelist()
def register_pull_transaction(mpesa_settings: str, callback_url: str | None = None) -> dict:
    """Register a shortcode with Safaricom for Pull Transactions.

    `callback_url` exists so bulk registration can capture the URL in the web
    request context and pass it down. Built from the request Host header,
    `build_callback_url` silently falls back to conf.host_name (or
    http://<site>) when there is no request - and since Safaricom rejects
    re-registration with "Shortcode already Registered!", a wrong URL baked in
    from a background job cannot be corrected afterwards.
    """
    import requests as _requests

    from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import get_token
    from frappe_mpsa_payments.utils.utils import build_callback_url

    settings = frappe.get_doc("Mpesa Settings", mpesa_settings)

    if not settings.pull_transaction_nominated_number:
        frappe.throw(_("Pull Transaction Nominated Number is required."))

    if not re.match(r"^254\d{9}$", settings.pull_transaction_nominated_number):
        frappe.throw(
            _(
                "Pull Transaction Nominated Number must be in the format 254XXXXXXXXX (254 followed by 9 digits)."
            )
        )

    base_url = (
        "https://sandbox.safaricom.co.ke"
        if settings.sandbox
        else "https://api.safaricom.co.ke"
    )

    try:
        token = get_token(
            app_key=settings.consumer_key,
            app_secret=settings.get_password("consumer_secret"),
            base_url=base_url,
        )
        shortcode = (
            settings.till_number if settings.sandbox else settings.business_shortcode
        )
        callback_url = callback_url or build_callback_url(PULL_REGISTRATION_CALLBACK)

        payload = {
            "ShortCode": shortcode,
            "RequestType": "Pull",
            "NominatedNumber": settings.pull_transaction_nominated_number,
            "CallBackURL": callback_url,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        resp = _requests.post(
            f"{base_url}/pulltransactions/v1/register",
            headers=headers,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        frappe.log_error(title="Mpesa Pull Transaction Registration Error", message=str(e))
        _record_registration_outcome(mpesa_settings, "Failed", str(e))
        return {"status": "error", "message": str(e)}

    if data.get("Response Status") == "1000":
        description = data.get("Response Description", "Registered successfully")
        _record_registration_outcome(mpesa_settings, "Registered", description)
        return {"status": "success", "message": description}

    description = data.get("Response Description", "Registration failed")

    # Safaricom rejects a second registration for a shortcode already enrolled.
    # That is not a failure to act on - it means the shortcode is already set up.
    if "already registered" in str(description).lower():
        _record_registration_outcome(mpesa_settings, "Already Registered", description)
        return {"status": "already_registered", "message": description}

    frappe.log_error(
        title="Mpesa Pull Transaction Registration Error", message=str(data)
    )
    _record_registration_outcome(mpesa_settings, "Failed", description)
    return {"status": "error", "message": description}


@frappe.whitelist()
def bulk_register_pull_transactions(
    settings_names: str | list | None = None, dry_run: int | bool = 0
) -> dict:
    """Queue pull registration for many shortcodes at once.

    Registration is effectively one-way (Safaricom rejects re-registration), so
    the CallBackURL is resolved here, inside the web request, and passed to the
    worker rather than rebuilt there from an unreliable fallback.
    """
    from frappe_mpsa_payments.utils.utils import build_callback_url

    frappe.only_for(["System Manager", "Administrator"])

    if isinstance(settings_names, str):
        settings_names = frappe.parse_json(settings_names)

    if not settings_names:
        settings_names = [
            row.name
            for row in frappe.get_all(
                "Mpesa Settings",
                filters={"pull_transaction_nominated_number": ["!=", ""]},
                fields=["name"],
            )
        ]

    if not settings_names:
        return {"status": "error", "message": _("No Mpesa Settings to register.")}

    try:
        callback_url = build_callback_url(PULL_REGISTRATION_CALLBACK)
    except RuntimeError:
        # build_callback_url reads the request Host header and raises
        # "object is not bound" with no request. Better to stop here than to
        # register 60+ shortcodes against a guessed hostname we cannot correct.
        frappe.throw(
            _(
                "Bulk pull registration must be run from the web interface so the "
                "callback URL can be read from the request. Registration cannot be "
                "undone, so it will not run with a guessed hostname."
            )
        )

    if frappe.utils.cint(dry_run):
        return {
            "status": "dry_run",
            "message": _("Would register {0} shortcode(s) with callback {1}").format(
                len(settings_names), callback_url
            ),
            "count": len(settings_names),
            "callback_url": callback_url,
            "settings": settings_names,
        }

    frappe.enqueue(
        "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings"
        ".mpesa_settings.execute_bulk_pull_registration",
        queue="long",
        timeout=3600,
        settings_names=settings_names,
        callback_url=callback_url,
    )

    return {
        "status": "success",
        "message": _("Queued registration for {0} shortcode(s).").format(
            len(settings_names)
        ),
        "count": len(settings_names),
        "callback_url": callback_url,
    }


def execute_bulk_pull_registration(settings_names: list, callback_url: str) -> None:
    """Register each shortcode in turn, isolating failures so one cannot abort the batch."""
    registered, already, failed = [], [], []

    for name in settings_names:
        try:
            result = register_pull_transaction(name, callback_url=callback_url)
            status = result.get("status")
            if status == "success":
                registered.append(name)
            elif status == "already_registered":
                already.append(name)
            else:
                failed.append(f"{name}: {result.get('message')}")
        except Exception as e:
            # Includes the frappe.throw for a missing nominated number.
            failed.append(f"{name}: {e}")
            _record_registration_outcome(name, "Failed", str(e))

        frappe.db.commit()
        time.sleep(BULK_REGISTRATION_DELAY_SECONDS)

    summary = (
        f"Registered: {len(registered)}\n"
        f"Already registered: {len(already)}\n"
        f"Failed: {len(failed)}\n\n"
        f"Callback URL used: {callback_url}\n\n"
        f"Newly registered: {registered}\n\n"
        f"Failures:\n" + "\n".join(failed)
    )
    frappe.log_error(title="Mpesa Bulk Pull Registration Complete", message=summary)

    frappe.publish_realtime(
        event="mpesa_bulk_pull_registration_complete",
        message={
            "status": "success" if not failed else "warning",
            "title": "Bulk Pull Registration Complete",
            "message": (
                f"{len(registered)} registered, {len(already)} already registered, "
                f"{len(failed)} failed."
            ),
        },
        user=frappe.session.user,
    )


@frappe.whitelist(allow_guest=True)
def pull_transaction_callback(**kwargs) -> dict:
    frappe.log_error(
        title="Mpesa Pull Transaction Callback",
        message=frappe.as_json(kwargs),
    )
    return {"ResultCode": 0, "ResultDesc": "Accepted"}
