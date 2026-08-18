# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry import (
    create_and_reconcile_payment_reconciliation,
    create_payment_entry,
    get_outstanding_invoices,
)

# Used when Mpesa Settings has no Reconciliation Priority rows. Reproduces the
# order this matching was hardcoded to before the table drove it, so an install
# that never configured the table keeps behaving the same.
DEFAULT_RECONCILIATION_ORDER = (
    ("Sales Invoice", "name"),
    ("Sales Order", "name"),
    ("Quotation", "name"),
    ("Customer", "name"),
)

# Where a matched document names its customer. Anything not listed is assumed
# to carry a plain `customer` field.
CUSTOMER_FIELD_BY_DOCTYPE = {
    "Customer": "name",
    "Quotation": "party_name",
}
DEFAULT_CUSTOMER_FIELD = "customer"


class MpesaC2BPaymentRegister(Document):
    def before_insert(self):
        if self.thirdpartytransid:
            er = frappe.db.get_value(
                "Mpesa Express Request",
                {"merchant_request_id": self.thirdpartytransid},
                ["name", "status"],
                as_dict=True,
            )
            if er:
                frappe.log_error(
                    message=f"Blocked C2B because matching Express Request {er.name} "
                    f"already exists (status={er.status})",
                    title="Mpesa C2B Insert Blocked",
                )
                frappe.throw(
                    _(
                        "Cannot record C2B payment: Express Request {0} already exists"
                    ).format(er.name)
                )

        if self.transid:
            er2 = frappe.db.get_value(
                "Mpesa Express Request",
                {"transaction_id": self.transid},
                ["name", "status"],
                as_dict=True,
            )
            if er2:
                frappe.log_error(
                    message=f"Duplicate C2B payment detected: {self.transid} "
                    f"already exists in Express Request {er2.name}",
                    title="Mpesa C2B Duplicate",
                )
                frappe.throw(
                    _(
                        "Cannot record C2B payment: Transaction already captured in Express Request {0}"
                    ).format(er2.name)
                )

        self.set_missing_values()

    def after_insert(self):
        try:
            auto_reconcile = frappe.db.get_value(
                "Mpesa Settings",
                {"business_shortcode": self.businessshortcode},
                "auto_reconcile_c2b",
            )

            if not auto_reconcile:
                return

            self.db_set("submit_payment", 1)
            self.submit()

        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"C2B Auto-submit Error: {str(e)}")

    def set_missing_values(self):
        self.currency = "KES"
        self.full_name = self.full_name = " ".join(
            filter(None, [self.firstname, self.middlename, self.lastname])
        )

        if "erpnext" in frappe.get_installed_apps():
            register_url_list = frappe.get_all(
                "Mpesa C2B Payment Register URL",
                filters={
                    "business_shortcode": self.businessshortcode,
                    "register_status": "Success",
                },
                fields=["company", "mode_of_payment"],
            )
            if len(register_url_list) > 0:
                self.company = register_url_list[0].company
                self.mode_of_payment = register_url_list[0].mode_of_payment

            if self.billrefnumber and not self.customer:
                self._find_customer_from_billref(self.billrefnumber)

    def before_submit(self):
        if not self.transamount:
            frappe.throw(_("Trans Amount is required"))
        if "erpnext" not in frappe.get_installed_apps():
            return
        if not self.company:
            frappe.throw(_("Company is required"))
        if not self.customer:
            frappe.throw(_("Customer is required"))
        if not self.mode_of_payment:
            frappe.throw(_("Mode of Payment is required"))

        if self.submit_payment:
            refs = []
            invoice, order = self._get_matching_refs()

            if invoice:
                refs.append(
                    {
                        "reference_doctype": "Sales Invoice",
                        "reference_name": invoice,
                        "allocated_amount": self.transamount,
                    }
                )

            elif order:
                refs.append(
                    {
                        "reference_doctype": "Sales Order",
                        "reference_name": order,
                        "allocated_amount": self.transamount,
                    }
                )

            payment_entry = create_payment_entry(
                self.company,
                self.customer,
                self.transamount,
                self.currency,
                self.mode_of_payment,
                "Customer",
                self.posting_date,
                self.name,
                self.posting_date,
                None,
                self.submit_payment,
                references=refs or None,
            )

            self.payment_entry = payment_entry.name

    def on_submit(self):
        if self.transid:
            exists = frappe.db.exists(
                "Mpesa Express Request", {"transaction_id": self.transid}
            )
            if exists:
                frappe.db.savepoint("before_c2b_submit")
                try:
                    self.cancel()
                    frappe.throw(
                        _(
                            "Duplicate C2B transaction {0}. Cancelled automatically."
                        ).format(self.transid)
                    )
                except Exception:
                    frappe.db.rollback(save_point="before_c2b_submit")
                    raise

        if frappe.db.get_global("is_manual_reconciliation") == "1":
            return

        try:
            self._reconcile_payment()

        except Exception as e:
            frappe.log_error(
                frappe.get_traceback(), f"C2B Reconciliation Error: {str(e)}"
            )

    def on_cancel(self):
        """Ensure linked Payment Entry is also cancelled when this record is cancelled."""
        if self.payment_entry:
            try:
                pe = frappe.get_doc("Payment Entry", self.payment_entry)
                if pe.docstatus == 1:
                    pe.cancel()
                    frappe.msgprint(
                        _("Linked Payment Entry {0} cancelled").format(pe.name),
                        alert=True,
                    )
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Failed to cancel Payment Entry {self.payment_entry} for C2B {self.name}",
                )

    def _reconciliation_order(self) -> list[tuple[str, str]]:
        """The configured priority for this shortcode.

        Falls back to the historical hardcoded order when the table is empty,
        so an unconfigured install is unaffected.

        The table only appears in the form while auto_reconcile_c2b is on, so
        it stays dormant while that is off - otherwise rows left behind by
        someone who disabled auto reconciliation would quietly keep steering
        customer resolution, which runs whether or not the toggle is set.
        """
        settings = frappe.db.get_value(
            "Mpesa Settings",
            {"business_shortcode": self.businessshortcode},
            ["name", "auto_reconcile_c2b"],
            as_dict=True,
        )

        settings_name = (
            settings.name if settings and settings.auto_reconcile_c2b else None
        )

        rows = []
        if settings_name:
            rows = [
                (row.target_doctype, row.match_field or "name")
                for row in frappe.get_all(
                    "Mpesa Reconciliation Priority",
                    filters={
                        "parent": settings_name,
                        "parenttype": "Mpesa Settings",
                    },
                    fields=["target_doctype", "match_field"],
                    order_by="idx asc",
                )
                if row.target_doctype
            ]

        return rows or list(DEFAULT_RECONCILIATION_ORDER)

    def _match_filters(self, doctype: str, match_field: str) -> dict:
        filters = {match_field: self.billrefnumber}

        meta = frappe.get_meta(doctype)
        if meta.is_submittable:
            filters["docstatus"] = 1

        # A Quotation can be addressed to a Lead, whose party_name is not a
        # Customer. Only Customer quotations can name one.
        if doctype == "Quotation" and meta.has_field("quotation_to"):
            filters["quotation_to"] = "Customer"

        return filters

    def _usable_match(self, doctype: str, match_field: str) -> tuple[str, str] | None:
        """Guard a configured row against a doctype or field that no longer exists."""
        if not doctype:
            return None

        try:
            meta = frappe.get_meta(doctype)
        except Exception:
            # A configured row can outlive the doctype or app it points at.
            return None

        if match_field != "name" and not meta.has_field(match_field):
            return None

        customer_field = CUSTOMER_FIELD_BY_DOCTYPE.get(doctype, DEFAULT_CUSTOMER_FIELD)
        if customer_field != "name" and not meta.has_field(customer_field):
            return None

        return match_field, customer_field

    def _find_customer_from_billref(self, billrefnumber: str) -> str | None:
        if not billrefnumber:
            return

        for doctype, match_field in self._reconciliation_order():
            usable = self._usable_match(doctype, match_field)
            if not usable:
                continue

            match_field, customer_field = usable
            customer = frappe.db.get_value(
                doctype, self._match_filters(doctype, match_field), customer_field
            )
            if customer:
                self.customer = customer
                return

    def _reconcile_payment(self):
        settings = frappe.get_cached_value(
            "Mpesa Settings",
            {"business_shortcode": self.businessshortcode},
            ["auto_reconcile_c2b", "auto_create_sales_invoice"],
            as_dict=True,
        )

        if not settings.auto_reconcile_c2b or not self.payment_entry:
            return

        invoice, order = self._get_matching_refs()

        if order and settings.auto_create_sales_invoice:
            self._create_sales_invoice_from_order(order)

        else:
            # Fallback: FIFO
            outstanding_invoices = get_outstanding_invoices(
                customer=self.customer, company=self.company
            )

            if outstanding_invoices:
                self._reconcile_against_invoice(outstanding_invoices)

    def _get_matching_refs(self):
        """
        Returns a tuple (invoice, sales_order), where exactly one is non-None if billrefnumber matched either.
        Otherwise both are None.

        Walks the configured Reconciliation Priority in order. Rows targeting
        anything we cannot settle a payment against are skipped here; they still
        count for customer resolution.
        """
        for doctype, match_field in self._reconciliation_order():
            if not self._usable_match(doctype, match_field):
                continue

            if doctype == "Sales Invoice":
                invoice = self._find_matching_invoice(match_field)
                if invoice:
                    return invoice, None

            elif doctype == "Sales Order":
                order = self._find_matching_sales_order(match_field)
                if order:
                    return None, order

        return None, None

    def _find_matching_invoice(self, match_field: str = "name"):
        if not self.billrefnumber:
            return None

        return frappe.get_value(
            "Sales Invoice",
            {
                match_field: self.billrefnumber,
                "docstatus": 1,
                "company": self.company,
                "customer": self.customer,
                "outstanding_amount": (">", 0),
            },
            "name",
        )

    def _find_matching_sales_order(self, match_field: str = "name"):
        if not self.billrefnumber:
            return None

        return frappe.get_value(
            "Sales Order",
            {
                match_field: self.billrefnumber,
                "docstatus": 1,
                "company": self.company,
                "customer": self.customer,
                "status": ("not in", ["Closed", "Completed"]),
            },
            "name",
        )

    def _create_sales_invoice_from_order(self, sales_order):
        try:
            from erpnext.selling.doctype.sales_order.sales_order import (
                make_sales_invoice,
            )

            si = make_sales_invoice(sales_order)
            # si.allocate_advances_automatically = True
            si.insert(ignore_permissions=True)
            si.submit()

            return si.name

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Sales Invoice Creation Failed for SO {sales_order}",
            )
            return None

    def _reconcile_against_invoice(self, invoice_list):
        if isinstance(invoice_list, str):
            invoice_list = [invoice_list]

        create_and_reconcile_payment_reconciliation(
            outstanding_invoices=invoice_list,
            customer=self.customer,
            company=self.company,
            payment_entries=[self.payment_entry],
        )
