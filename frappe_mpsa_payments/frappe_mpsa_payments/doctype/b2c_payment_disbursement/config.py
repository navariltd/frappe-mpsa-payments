from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass
class DoctypeConfig:
    fields: List[str]
    date_field: str
    additional_filters: Dict
    use_erpnext_function: bool = False
    payable_amount_calc: Optional[Callable[[Dict], float]] = None
    invoice_amount_field: str = "grand_total"


# Configuration for different document types
DOCTYPE_CONFIGS = {
    "Employee Advance": DoctypeConfig(
        fields=[
            "name",
            "posting_date",
            "employee",
            "currency",
            "advance_amount",
            "paid_amount",
            "pending_amount",
            "claimed_amount",
        ],
        date_field="posting_date",
        additional_filters={},
        payable_amount_calc=lambda e: (e.advance_amount or 0) - (e.paid_amount or 0),
        invoice_amount_field="advance_amount",
    ),
    "Expense Claim": DoctypeConfig(
        fields=[
            "name",
            "posting_date",
            "employee",
            "grand_total",
            "total_claimed_amount",
            "total_amount_reimbursed",
        ],
        date_field="posting_date",
        additional_filters={"approval_status": "Approved", "status": "Unpaid"},
        payable_amount_calc=lambda e: (e.total_claimed_amount or 0)
        - (e.total_amount_reimbursed or 0),
        invoice_amount_field="total_claimed_amount",
    ),
    "Purchase Invoice": DoctypeConfig(
        fields=[],
        date_field="posting_date",
        additional_filters={},
        use_erpnext_function=True,
        payable_amount_calc=lambda e: e.get("outstanding_amount", 0),
        invoice_amount_field="invoice_amount",
    ),
    "Purchase Order": DoctypeConfig(
        fields=[],
        date_field="transaction_date",
        additional_filters={},
        use_erpnext_function=True,
        payable_amount_calc=lambda e: e.get("outstanding_amount", 0),
        invoice_amount_field="invoice_amount",
    ),
    "Salary Slip": DoctypeConfig(
        fields=[
            "name",
            "posting_date",
            "employee",
            "net_pay",
            "currency",
            "journal_entry",
            "base_rounded_total",
            "payroll_entry",
        ],
        date_field="posting_date",
        additional_filters={},
        payable_amount_calc=lambda e: e.get("base_rounded_total")
        or e.get("rounded_total")
        or e.get("outstanding_amount")
        or 0,
        invoice_amount_field="net_pay",
    ),
    "Loan": DoctypeConfig(
        fields=[
            "name",
            "applicant_type",
            "applicant",
            "loan_amount",
            "disbursed_amount",
        ],
        date_field="posting_date",
        additional_filters={"status": ["in", ["Sanctioned", "Partially Disbursed"]]},
        payable_amount_calc=lambda e: (e.loan_amount or 0) - (e.disbursed_amount or 0),
        invoice_amount_field="loan_amount",
    ),
}
