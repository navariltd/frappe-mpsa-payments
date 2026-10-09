# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_request_site_address, get_url

from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import get_token
from frappe_mpsa_payments.frappe_mpsa_payments.api.callback_security import build_callback_urls

class MpesaC2BPaymentRegisterURL(Document):
    """Handle M-Pesa C2B URL registration with automatic API version fallback."""

    SANDBOX_URL = "https://sandbox.safaricom.co.ke"
    LIVE_URL = "https://api.safaricom.co.ke"
    API_VERSIONS = ["v2", "v1"]
    PRODUCT_MISMATCH_ERROR_CODE = "401.003.01"

    def validate(self):
        mpesa_settings = self._get_mpesa_settings()
        base_url = self._get_base_url(mpesa_settings)
        token = self._get_authentication_token(mpesa_settings, base_url)

        payload = self._build_registration_payload(mpesa_settings)
        headers = self._build_request_headers(token)

        success = self._register_urls_with_fallback(base_url, headers, payload)
        self.register_status = "Success" if success else "Failed"

    def _get_mpesa_settings(self) -> Document:
        return frappe.get_doc("Mpesa Settings", self.mpesa_settings)

    def _get_base_url(self, mpesa_settings: Document) -> str:
        return self.SANDBOX_URL if mpesa_settings.sandbox else self.LIVE_URL

    def _get_business_shortcode(self, mpesa_settings: Document) -> str:
        is_production = not mpesa_settings.sandbox
        return (
            mpesa_settings.business_shortcode
            if is_production
            else mpesa_settings.till_number
        )

    def _get_authentication_token(self, mpesa_settings: Document, base_url: str) -> str:
        return get_token(
            app_key=mpesa_settings.consumer_key,
            app_secret=mpesa_settings.get_password("consumer_secret"),
            base_url=base_url,
        )

    def _build_callback_urls(self) -> tuple[str, str]:
        from frappe_mpsa_payments.frappe_mpsa_payments.api.callback_security import build_callback_urls

        urls = build_callback_urls(self.mpesa_settings)
        return urls["validation"], urls["confirmation"]

    def _build_registration_payload(self, mpesa_settings: Document) -> dict:
        validation_url, confirmation_url = self._build_callback_urls()
        business_shortcode = self._get_business_shortcode(mpesa_settings)

        return {
            "ShortCode": business_shortcode,
            "ResponseType": "Completed",
            "ConfirmationURL": confirmation_url,
            "ValidationURL": validation_url,
        }

    def _build_request_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _build_register_url(self, base_url: str, version: str) -> str:
        return f"{base_url}/mpesa/c2b/{version}/registerurl"

    def _is_product_mismatch_error(self, response: dict) -> bool:
        error_code_match = response.get("errorCode") == self.PRODUCT_MISMATCH_ERROR_CODE
        text_match = "no apiproduct match found" in str(response).lower()
        return error_code_match or text_match

    def _is_success_response(self, response: dict) -> bool:
        return response.get("ResponseDescription") == "Success"

    def _log_failure(self, version: str, response: dict):
        frappe.log_error(
            f"M-Pesa C2B URL registration {version} failed.", f"Response: {response}"
        )

    def _log_error(self, version: str, error_type: str, error: Exception):
        frappe.log_error(
            "Mpesa C2B URL Registration Error",
            f"{error_type} with API {version}: {error}",
        )

    def _handle_http_error(
        self, error: requests.exceptions.HTTPError, version: str, is_last_version: bool
    ) -> bool:
        """
        Handle HTTP errors and determine if retry is needed.

        Returns:
            bool: True if should retry with next version, False otherwise
        """
        self._log_error(version, "HTTP Error", error)
        frappe.logger().error(f"Response Content: {error.response.content}")

        # Check if response contains product mismatch
        try:
            error_response = error.response.json()
            if self._is_product_mismatch_error(error_response):
                return True  # Retry with next version
        except Exception:
            pass

        if is_last_version:
            frappe.msgprint(f"Response Content: {error.response.content}")

        return not is_last_version

    def _handle_network_error(
        self, error: Exception, error_type: str, version: str, is_last_version: bool
    ):
        """Handle network-related errors (connection, timeout, etc.)."""
        self._log_error(version, error_type, error)

        if is_last_version:
            frappe.msgprint(f"{error_type}: {error}")

    def _make_registration_request(
        self, url: str, headers: dict, payload: dict
    ) -> dict:
        """
        Make HTTP request to M-Pesa API.

        Raises:
            requests.exceptions.RequestException: For any request-related errors
        """
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    def _attempt_registration(
        self, version: str, base_url: str, headers: dict, payload: dict
    ) -> tuple[bool, dict | None, bool]:
        """
        Attempt URL registration with a specific API version.

        Returns:
            Tuple containing:
                - bool: Success status
                - Optional[Dict]: Response or error object
                - bool: Whether to retry with next version
        """
        register_url = self._build_register_url(base_url, version)

        try:
            response = self._make_registration_request(register_url, headers, payload)

            # Check for product mismatch
            if self._is_product_mismatch_error(response):
                return False, response, True  # Retry with next version

            # Check for success
            if self._is_success_response(response):
                return True, response, False  # Success, no retry needed
            else:
                self._log_failure(version, response)
                frappe.msgprint(str(response))
                return False, response, True  # Retry with next version

        except requests.exceptions.HTTPError as error:
            is_last = version == self.API_VERSIONS[-1]
            should_retry = self._handle_http_error(error, version, is_last)
            return False, error, should_retry

        except requests.exceptions.ConnectionError as error:
            is_last = version == self.API_VERSIONS[-1]
            self._handle_network_error(error, "Connection Error", version, is_last)
            return False, error, not is_last

        except requests.exceptions.Timeout as error:
            is_last = version == self.API_VERSIONS[-1]
            self._handle_network_error(error, "Timeout Error", version, is_last)
            return False, error, not is_last

        except requests.exceptions.RequestException as error:
            is_last = version == self.API_VERSIONS[-1]
            self._handle_network_error(error, "Request Exception", version, is_last)
            return False, error, not is_last

    def _register_urls_with_fallback(
        self, base_url: str, headers: dict, payload: dict
    ) -> bool:
        """
        Register URLs with automatic fallback between API versions.

        Returns:
            bool: True if registration succeeded, False otherwise
        """
        last_error = None

        for version in self.API_VERSIONS:
            success, result, should_retry = self._attempt_registration(
                version, base_url, headers, payload
            )

            if success:
                return True

            last_error = result

            if not should_retry:
                break

        # All versions failed
        self._log_final_failure(last_error)
        self._show_final_error_message(last_error)
        return False

    def _log_final_failure(self, last_error: Exception | None):
        frappe.log_error(
            "M-Pesa C2B URL registration failed with all API versions.",
            f"Error: {last_error}",
        )

    def _show_final_error_message(self, last_error: Exception | None):
        frappe.msgprint(
            _("M-Pesa C2B URL registration failed."), _("Error: {0}").format(last_error)
        )
