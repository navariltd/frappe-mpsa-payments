from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Callable, Literal, Optional, Union

import frappe
import requests
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from requests.auth import HTTPBasicAuth


# Remote error handler for Mpesa
def on_mpesa_error(data, url, doctype, document_name):
    error_msg = f"Remote error at {url} for {doctype} {document_name}: {data}"
    frappe.log_error(
        title="Mpesa Error",
        message=error_msg,
        reference_doctype=doctype,
        reference_name=document_name,
    )


# Custom integration request updater
def update_integration_request(
    integration_request: str,
    status: Literal["Completed", "Failed"],
    output: str | None = None,
    error: str | None = None,
) -> None:
    doc = frappe.get_doc("Integration Request", integration_request, for_update=True)

    if error:
        doc.error = error if doc.error in (None, "null") else (doc.error + "\n" + error)

    if output:
        doc.output = (
            output if doc.output in (None, "null") else (doc.output + "\n" + output)
        )

    doc.status = status
    doc.save(ignore_permissions=True)


# Observer for error handling
class ErrorObserver:
    def update(self, notifier: BaseMpesaConnector):
        if notifier.error:
            name = getattr(notifier.integration_request, "name", None)
            if name:
                update_integration_request(
                    name, status="Failed", error=str(notifier.error)
                )
            frappe.log_error(
                title="Mpesa Fatal Error",
                message=str(notifier.error),
                reference_doctype=notifier.doctype,
                reference_name=notifier.document_name,
            )
            frappe.throw(
                "A Fatal Error occurred. Check the Error Log.",
                notifier.error,
                title="Mpesa Fatal Error",
            )


# Base builder class
class BaseMpesaConnector:
    def __init__(self):
        self.integration_request: str | Document | None = None
        self.error: str | Exception | None = None
        self._observers: list[ErrorObserver] = [ErrorObserver()]
        self.doctype: str | Document | None = None
        self.document_name: str | None = None

    def notify(self):
        for observer in self._observers:
            observer.update(self)


class MpesaConnector(BaseMpesaConnector):
    def __init__(self, settings_name: str):
        super().__init__()
        self.settings_name = settings_name
        self._endpoint: str | None = None
        self._payload: dict | None = None
        self._method: Literal["GET", "POST", "PATCH", "PUT"] | None = None
        self._success_callback: Callable | None = None
        self._error_callback: Callable | None = None
        self._request_description: str | None = None
        self._custom_headers: dict = {}
        self._base_url: str | None = None
        self._url: str | None = None
        self._timeout: int = 30
        self.integration_request = None
        self.doctype = self.document_name = self.error = None

    def _get_mpesa_settings(self) -> dict:
        if not self.settings_name:
            frappe.throw("Mpesa Settings name is required.", frappe.MandatoryError)

        s = frappe.get_doc("Mpesa Settings", self.settings_name)
        return {
            "consumer_key": s.consumer_key,
            "consumer_secret": s.get_password("consumer_secret"),
            "access_token": s.access_token,
            "token_expiry": s.token_expiry,
            "sandbox": s.sandbox,
        }

    def _initialize_settings(self):
        settings = self._get_mpesa_settings()
        self._base_url = (
            "https://sandbox.safaricom.co.ke"
            if settings["sandbox"]
            else "https://api.safaricom.co.ke"
        )

    def _is_token_valid(self) -> bool:
        settings = self._get_mpesa_settings()
        if not settings["access_token"] or not settings["token_expiry"]:
            return False
        return datetime.now() < settings["token_expiry"]

    def _update_token(self, token: str, expires_in: int):
        token_expiry = datetime.now() + timedelta(seconds=int(expires_in))
        frappe.db.set_value(
            "Mpesa Settings",
            self.settings_name,
            {
                "access_token": token,
                "expires_in": int(expires_in),
                "token_expiry": token_expiry,
            },
        )
        frappe.db.commit()

    def _set_timeout(self, seconds: int):
        self._timeout = seconds
        return self

    def authenticate(self) -> str:
        try:
            self._initialize_settings()
            s = self._get_mpesa_settings()
            url = f"{self._base_url}/oauth/v1/generate?grant_type=client_credentials"
            auth_string = f"{s['consumer_key']}:{s['consumer_secret']}"
            encoded_auth = base64.b64encode(auth_string.encode()).decode()

            self.integration_request = create_request_log(
                data={"grant_type": "client_credentials"},
                request_description="Mpesa OAuth Token Authentication",
                is_remote_request=True,
                service_name="Mpesa",
                request_headers={"Authorization": f"Basic {encoded_auth}"},
                url=url,
                reference_docname=self.settings_name,
                reference_doctype="Mpesa Settings",
            )
            r = requests.get(
                url, auth=HTTPBasicAuth(s["consumer_key"], s["consumer_secret"])
            )
            r.raise_for_status()
            data = r.json()
            self._update_token(data["access_token"], data["expires_in"])

            update_integration_request(
                self.integration_request.name, status="Completed", output=str(data)
            )

            return data["access_token"]
        except Exception as e:
            if hasattr(self, "integration_request") and self.integration_request:
                update_integration_request(
                    self.integration_request.name, status="Failed", error=str(e)
                )
            self.error = e
            self.notify()
            raise

    def _get_authenticated_headers(self) -> dict:
        if not self._is_token_valid():
            self.authenticate()
        token = self._get_mpesa_settings()["access_token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        headers.update(self._custom_headers)
        return headers

    def make_remote_call(
        self,
        doctype=None,
        document_name=None,
        retrying=False,
        skip_integration_request=False,
    ):
        if not all([self._endpoint, self._method, self._success_callback]):
            frappe.throw(
                "Missing required parameters (endpoint, method, callbacks).",
                frappe.MandatoryError,
            )

        self._initialize_settings()
        self._url = f"{self._base_url}/{self._endpoint.lstrip('/')}"
        self.doctype, self.document_name = doctype, document_name

        if not retrying and not skip_integration_request:
            reuse_existing = getattr(self, "_reuse_existing_integration_request", False)
            if reuse_existing:
                existing_request = frappe.get_all(
                    "Integration Request",
                    filters={
                        "reference_docname": document_name,
                        "reference_doctype": doctype,
                        "request_description": self._request_description,
                    },
                    fields=["name"],
                    order_by="creation desc",
                    limit=1,
                )
                if existing_request:
                    self.integration_request = frappe.get_doc(
                        "Integration Request", existing_request[0].name
                    )
                else:
                    self.integration_request = create_request_log(
                        data=self._payload,
                        request_description=self._request_description,
                        is_remote_request=True,
                        service_name="Mpesa",
                        request_headers=self._get_authenticated_headers(),
                        url=self._url,
                        reference_docname=document_name,
                        reference_doctype=doctype,
                    )
            else:
                self.integration_request = create_request_log(
                    data=self._payload,
                    request_description=self._request_description,
                    is_remote_request=True,
                    service_name="Mpesa",
                    request_headers=self._get_authenticated_headers(),
                    url=self._url,
                    reference_docname=document_name,
                    reference_doctype=doctype,
                )

        try:
            response = self._send_request()
            data = response.json()

            if response.status_code in {200, 201}:
                self._handle_success(data)
            else:
                self._handle_error(data)
            return data

        except requests.RequestException as e:
            self.error = e
            self.notify()
            return None

    def _send_request(self) -> requests.Response:
        headers = self._get_authenticated_headers()
        method_map = {
            "GET": lambda: requests.get(
                self._url, headers=headers, params=self._payload, timeout=self._timeout
            ),
            "POST": lambda: requests.post(
                self._url, json=self._payload, headers=headers, timeout=self._timeout
            ),
            "PATCH": lambda: requests.patch(
                self._url, json=self._payload, headers=headers, timeout=self._timeout
            ),
            "PUT": lambda: requests.put(
                self._url, json=self._payload, headers=headers, timeout=self._timeout
            ),
        }

        if self._method in {"PATCH", "PUT"} and self._payload and "id" in self._payload:
            rid = self._payload.pop("id")
            if f"/{rid}/" not in self._url:
                self._url = f"{self._url.rstrip('/')}/{rid}/"

        return method_map[self._method]()

    def _handle_success(self, data):
        self._success_callback(
            response=data,
            payload=self._payload,
            document_name=self.document_name,
            doctype=self.doctype,
            integration_request=self.integration_request,
            settings_name=self.settings_name,
        )
        update_integration_request(
            self.integration_request.name, status="Completed", output=str(data)
        )

    def _handle_error(self, data):
        update_integration_request(
            self.integration_request.name, status="Failed", error=str(data)
        )
        if self._error_callback:
            self._error_callback(
                response=data,
                payload=self._payload,
                document_name=self.document_name,
                doctype=self.doctype,
                integration_request=self.integration_request,
                settings_name=self.settings_name,
            )
        else:
            on_mpesa_error(
                data,
                url=self._url,
                doctype=self.doctype,
                document_name=self.document_name,
            )

    def set_headers(self, headers: dict):
        self._custom_headers.update(headers)
        return self

    def set_endpoint(self, endpoint: str):
        self._endpoint = endpoint
        return self

    def set_payload(self, payload: dict):
        self._payload = payload
        return self

    def set_method(self, method):
        self._method = method
        return self

    def on_success(self, callback: Callable):
        self._success_callback = callback
        return self

    def on_error(self, callback: Callable):
        self._error_callback = callback
        return self

    def describe(self, description: str):
        self._request_description = description
        return self

    def reuse_existing_request(self, flag: bool = True):
        self._reuse_existing_integration_request = flag
        return self


def get_response_data(response: requests.Response) -> Optional[Union[dict, str, bytes]]:
    """Extract response data based on content type"""
    content_type = response.headers.get("Content-Type", "").lower()
    if "json" in content_type:
        return response.json()
    if "text" in content_type or "xml" in content_type:
        return response.text.strip() or None
    if any(mime in content_type for mime in ["octet-stream", "pdf", "zip"]):
        return response.content
    return None
