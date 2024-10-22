// Copyright (c) 2024, Navari Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa Payments", {
  onload(frm) {
    const default_company = frappe.defaults.get_user_default("Company");
    frm.set_value("company", default_company);
  },

  refresh(frm) {
    frm.disable_save();

    frm.set_df_property("invoices", "cannot_add_rows", true);
    frm.set_df_property("mpesa_payments", "cannot_add_rows", true);

    let fetch_btn = frm.add_custom_button(__("Fetch Entries"), () => {
      frm.trigger("fetch_entries");
    });

  },

  fetch_entries(frm) {
    frm.clear_table("invoices");
    frm.clear_table("mpesa_payments");

    // Fetch outstanding invoices
    frappe.call({
      method:
        "frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry.get_outstanding_invoices",
      args: {
        company: frm.doc.company,
        currency: frm.doc.currency,
        customer: frm.doc.customer,
      },
      callback: function (response) {
        let draft_invoices = response.message;
        if (draft_invoices && draft_invoices.length > 0) {
          frm.clear_table("invoices");

          draft_invoices.forEach(function (invoice) {
            let row = frm.add_child("invoices");
            row.invoice = invoice.voucher_no;
            row.date = invoice.posting_date;
            row.total = invoice.invoice_amount;
            row.outstanding_amount = invoice.outstanding_amount;
          });

          frm.refresh_field("invoices");
        } else {
          frappe.msgprint({
            title: __("No Outstanding Invoices"),
            message: __(
              "No outstanding invoices were found for the selected customer."
            ),
            indicator: "orange",
          });
        }

        check_for_process_payments_button(frm);
      },
    });

    // Fetch draft payments
    frappe.call({
      method:
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.get_mpesa_draft_c2b_payments",
      args: {
        company: frm.doc.company,
        full_name: frm.doc.full_name || "",
      },
      callback: function (response) {
        let draft_payments = response.message;

        if (draft_payments && draft_payments.length > 0) {
          frm.clear_table("mpesa_payments");

          draft_payments.forEach(function (payment) {
            let row = frm.add_child("mpesa_payments");
            row.payment_id = payment.name;
            row.full_name = payment.full_name;
            row.date = payment.posting_date;
            row.amount = payment.transamount;
          });

          frm.refresh_field("mpesa_payments");
        } else {
          frappe.msgprint({
            title: __("No Outstanding Payments"),
            message: __(
              "No outstanding payments were found for the selected customer."
            ),
            indicator: "orange",
          });
        }

        check_for_process_payments_button(frm);
      },
    });
  },

  process_payments(frm) {
    let mpesa_payments = frm.doc.mpesa_payments;

    mpesa_payments.forEach(function (payment) {
      frappe.call({
        method: "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.submit_mpesa_payment",
        args: {
          mpesa_payment: payment.payment_id,
          customer: frm.doc.customer,
        },
        callback: function (response) {
          frappe.msgprint({
            message: __("Payment processed successfully"),
            indicator: "green",
          });
        },
      });
    });
  }
});

function check_for_process_payments_button(frm) {
  if (frm.doc.invoices.length > 0 && frm.doc.mpesa_payments.length > 0) {
    let process_btn = frm.add_custom_button(__("Process Payments"), () => {
      frm.trigger("process_payments");
    });

    process_btn.addClass("btn-primary");
  }
}