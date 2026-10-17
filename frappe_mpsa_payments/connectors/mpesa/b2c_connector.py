import base64
import json
from datetime import datetime, timedelta
from enum import Enum

import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime

from ...utils.doctype_names import B2C_REQUEST_DOCTYPE, MPESA_SETTINGS_DOCTYPE
from ..abstract import B2CConnector
from ..base_connector import (
    BaseAPIConnector,
    create_request_log,
    update_integration_request,
)


class BaseUrl(Enum):
    SANDBOX_URL = "https://sandbox.safaricom.co.ke"
    LIVE_URL = "https://api.safaricom.co.ke"


class MpesaB2CConnector(BaseAPIConnector, B2CConnector):
    """
    Handles OAuth token + B2C endpoint for Mpesa using BaseAPIConnector hooks.
    """

    provider_name = "Mpesa"

    def __init__(self, settings_name: str):
        super().__init__(
            provider="Mpesa",
            settings_doctype=MPESA_SETTINGS_DOCTYPE,
            settings_name=settings_name,
        )
        self._load_settings()

    def _load_settings(self):
        s = frappe.get_doc(
            self.settings_doctype, self.settings_name, ignore_permissions=True
        )
        self.consumer_key = s.consumer_key
        self.consumer_secret = s.get_password("consumer_secret")
        self.initiator_name = s.initiator_name
        self.security_credential = s.security_credential
        self.business_shortcode = s.business_shortcode
        self._token = s.access_token
        self._expiry = s.token_expiry
        self._base_url = (
            BaseUrl.SANDBOX_URL.value if s.sandbox else BaseUrl.LIVE_URL.value
        )

    def _is_token_valid(self) -> bool:
        return self._token and self._expiry and datetime.now() < self._expiry

    def _refresh_token(self):
        """Fetch a new OAuth token and persist it in settings."""
        endpoint = "/oauth/v1/generate?grant_type=client_credentials"
        auth_str = f"{self.consumer_key}:{self.consumer_secret}"
        basic_token = base64.b64encode(auth_str.encode()).decode()

        oauth_connector = MpesaB2CConnector(self.settings_name)
        oauth_connector.describe("Mpesa OAuth").set_endpoint(endpoint).set_method(
            "GET"
        ).set_headers({"Authorization": f"Basic {basic_token}"}).reuse_existing_request(
            False
        )

        data = oauth_connector.make_remote_call(self._base_url)

        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 0))
        expiry = datetime.now() + timedelta(seconds=expires_in)

        frappe.db.set_value(
            self.settings_doctype,
            self.settings_name,
            {"access_token": token, "expires_in": expires_in, "token_expiry": expiry},
        )
        frappe.db.commit()

        self._token = token
        self._expiry = expiry

    def _get_valid_token(self) -> str:
        if not self._is_token_valid():
            self._refresh_token()
        return self._token

    def send_b2c_request(self, b2c_request_doc: Document, callback_url: str) -> dict:
        """
        Dispatch a B2C payment request. Returns the raw JSON response.
        """

        token = self._get_valid_token()

        self.on_success(self._handle_success).on_error(self._handle_error)

        payload = {
            "InitiatorName": self.initiator_name,
            "SecurityCredential": self.security_credential,
            "CommandID": b2c_request_doc.commandid,
            "Amount": b2c_request_doc.amount,
            "PartyA": self.business_shortcode,
            "PartyB": b2c_request_doc.phone_number,
            "Remarks": b2c_request_doc.remarks,
            "QueueTimeOutURL": callback_url,
            "ResultURL": callback_url,
            "Occasion": b2c_request_doc.occassion,
            "OriginatorConversationID": b2c_request_doc.originator_conversation_id,
        }

        return (
            self.describe("Mpesa B2C")
            .set_endpoint("/mpesa/b2c/v3/paymentrequest")
            .set_method("POST")
            .set_headers(
                {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            )
            .set_payload(payload)
            .make_remote_call(
                self._base_url,
                doctype=b2c_request_doc.doctype,
                document_name=b2c_request_doc.name,
            )
        )

    def _handle_success(
        self,
        response=None,
        data=None,
        doctype=None,
        document_name=None,
        integration_request=None,
        settings_name=None,
        **kwargs,
    ):
        try:
            response_code = data.get("ResponseCode")

            status = "Initiated" if str(response_code) == "0" else "Failed"

            fields = {
                "conversation_id": data.get("ConversationID", ""),
                "originator_conversation_id": data.get("OriginatorConversationID", ""),
                "response_code": response_code,
                "response_description": data.get("ResponseDescription", ""),
                "status": status,
            }

            if frappe.db.exists(B2C_REQUEST_DOCTYPE, document_name):
                for key, value in fields.items():
                    frappe.db.set_value(B2C_REQUEST_DOCTYPE, document_name, key, value)
                frappe.log(f"B2C Request updated for {document_name}")

            update_integration_request(
                integration_request.name, status="Completed", output=str(data)
            )
            frappe.db.commit()
            frappe.publish_realtime(
                event="refresh_form", doctype=B2C_REQUEST_DOCTYPE, docname=document_name
            )

        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"B2C Request Success Error for {document_name}"
            )
            raise

    def _handle_error(
        self,
        response=None,
        data=None,
        error=None,
        doctype=None,
        document_name=None,
        integration_request=None,
        settings_name=None,
        **kwargs,
    ):
        if error is not None:
            frappe.log_error(
                title="Mpesa B2C Transport Error",
                message=frappe.get_traceback() or str(error),
            )
            update_integration_request(
                integration_request.name, status="Failed", output=str(error)
            )
            frappe.db.commit()
            return

        fields = {
            "status": "Failed",
            "error_code": data.get("errorCode", ""),
            "error_message": data.get("errorMessage", ""),
        }

        if frappe.db.exists(B2C_REQUEST_DOCTYPE, document_name):
            for key, val in fields.items():
                frappe.db.set_value(B2C_REQUEST_DOCTYPE, document_name, key, val)

        update_integration_request(
            integration_request.name, status="Failed", output=str(data)
        )
        frappe.db.commit()
        frappe.enqueue(
            "frappe_mpsa_payments.services.b2c_response_service.update_b2c_reference_status",
            queue="short",
            timeout=300,
            b2c_request_name=document_name,
            enqueue_next=True,
        )
        frappe.publish_realtime(
            event="refresh_form", doctype=B2C_REQUEST_DOCTYPE, docname=document_name
        )

    def handle_callback(self, payload: dict) -> None:
        """
        Process the full B2C callback payload, update the request doc,
        log to Integration Request, and enqueue downstream jobs.
        """

        result = frappe._dict(payload)
        occ_id = result.get("OriginatorConversationID")
        req = frappe.get_doc(
            B2C_REQUEST_DOCTYPE, {"originator_conversation_id": occ_id}
        )

        self.integration_request = create_request_log(
            data=payload,
            request_description="Mpesa B2C Callback",
            is_remote_request=True,
            service_name=self.provider,
            request_headers={},
            url=f"{self._base_url}/b2c/callback",
            reference_doctype=req.doctype,
            reference_docname=req.name,
        )

        params = result.get("ResultParameters", {}).get("ResultParameter", [])
        result_dict = {p.get("Key"): p.get("Value") for p in params}

        update_map = {
            "status": "Paid" if str(result.get("ResultCode")) == "0" else "Failed",
            "error_message": "" if str(result.get("ResultCode")) == "0" else result.get("ResultDesc"),
            "result_code": result.get("ResultCode"),
            "result_desc": result.get("ResultDesc"),
            "conversation_id": result.get("ConversationID"),
            "transaction_id": result.get("TransactionID"),
            "recipient_is_registered_customer": result_dict.get(
                "B2CRecipientIsRegisteredCustomer"
            ),
            "charges_paid_acct_avlbl_funds": result_dict.get(
                "B2CChargesPaidAccountAvailableFunds"
            ),
            "utility_acct_avlbl_funds": result_dict.get(
                "B2CUtilityAccountAvailableFunds"
            ),
            "working_acct_avlbl_funds": result_dict.get(
                "B2CWorkingAccountAvailableFunds"
            ),
            "receiver_public_name": result_dict.get("ReceiverPartyPublicName"),
        }

        tcd = result_dict.get("TransactionCompletedDateTime")
        if tcd:
            update_map["transaction_completed_datetime"] = get_datetime(tcd)

        for k, v in update_map.items():
            if v is not None:
                setattr(req, k, v)
        req.save(ignore_permissions=True)

        if self.integration_request:
            update_integration_request(
                self.integration_request.name,
                # Integration Request has no 'Paid' status; map it to Completed
                status="Completed" if req.status == "Paid" else "Failed",
                output=json.dumps(payload),
            )
        else:
            frappe.log_error(f"No integration_request on connector for {req.name}")

        frappe.enqueue(
            "frappe_mpsa_payments.services.b2c_response_service.update_b2c_reference_status",
            queue="short",
            timeout=300,
            b2c_request_name=req.name,
            enqueue_next=True,
        )
