# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

import uuid

import frappe
from frappe.model.document import Document

from ...api.b2c import make_b2c_payment_request


class B2CDisbursementRequest(Document):
    """B2C Payments Transactions"""

    def before_insert(self) -> None:
        """Insert Missing Values"""
        self.originator_conversation_id = self._generate_uuid_v4()
        self._set_payment_details()

    def validate(self) -> None:
        """B2C Payments Transactions validations"""
        if not self.originator_conversation_id:
            self.originator_conversation_id = self._generate_uuid_v4()

        if self.stanbic_settings and not self.dbs_reference_id:
            self.dbs_reference_id = self._generate_uuid_v4()

    def on_submit(self):
        make_b2c_payment_request(self.name)

    def _generate_uuid_v4(self) -> str:
        return str(uuid.uuid4())

    # What the payment is for, as Safaricom records it (max 100 chars each)
    REMARK_LABELS = {
        "Expense Claim": "Expense claim",
        "Employee Advance": "Employee advance",
        "Salary Slip": "Salary",
        "Purchase Invoice": "Supplier invoice",
        "Purchase Order": "Purchase order",
        "Loan": "Loan disbursement",
    }

    def _set_payment_details(self) -> None:
        """Derive Remarks, Occasion and CommandID from the document being paid,
        instead of the DocType defaults ('here are my remarks', SalaryPayment)."""
        label = self.REMARK_LABELS.get(self.reference_doctype, self.reference_doctype or "Payment")
        self.remarks = f"{label} {self.reference_name or ''}".strip()[:100]
        self.occassion = (self.b2c_payment_disbursement or "")[:100]
        self.commandid = (
            "SalaryPayment" if self.reference_doctype == "Salary Slip" else "BusinessPayment"
        )

    @frappe.whitelist()
    def retry_failed_payment(self) -> None:
        """Retry only failed payments"""
        make_b2c_payment_request(self.name)
