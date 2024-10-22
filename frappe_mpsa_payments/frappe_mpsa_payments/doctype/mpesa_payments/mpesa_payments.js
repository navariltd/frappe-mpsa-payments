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

  process_payments(frm, retryCount = 0) {
    // Maximum retry attempts
    const MAX_RETRIES = 3;
    const DELAY_BETWEEN_REQUESTS = 500; // 0.5 second delay between processing each payment

    let unpaid_invoices = frm.doc.invoices || [];
    let mpesa_payments = frm.doc.mpesa_payments || [];

    if (unpaid_invoices.length === 0 || mpesa_payments.length === 0) {
      frappe.msgprint({
        title: __("No Entries Found"),
        message: __("Please add at least one invoice and one Mpesa payment for processing."),
        indicator: "orange",
      });
      return;
    }

    // Recursive function to process each invoice one by one
    const processSingleInvoice = (invoiceIndex = 0) => {
      if (invoiceIndex >= unpaid_invoices.length) {

        frm.clear_table("invoices");
        frm.clear_table("mpesa_payments");
        frm.refresh_field("invoices");
        frm.refresh_field("mpesa_payments");

        frm.events.fetch_entries(frm);

        return;
      }

      // Get the current invoice
      let invoice = unpaid_invoices[invoiceIndex];
      let invoiceName = invoice.invoice;

      // Process all payments for this invoice
      mpesa_payments.forEach(function (payment, paymentIndex) {
        let paymentName = payment.payment_id;

        setTimeout(() => {
          frappe.call({
            method: "frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry.process_mpesa_c2b_reconciliation",
            args: {
              invoice_name: invoiceName,
              mpesa_name: paymentName,
            },
            callback: function (response) {
              if (response.message === "success") {
                frappe.msgprint({
                  title: __("Payment Processed"),
                  message: __("Invoice {0} and Payment {1} processed successfully.", [invoiceName, paymentName]),
                  indicator: "green",
                });
              } 
            //   else {
            //     frappe.msgprint({
            //       title: __("Payment Processing Failed"),
            //       message: __(
            //         "The payment could not be processed for Invoice {0} and Payment {1}. Please try again.",
            //         [invoiceName, paymentName]
            //       ),
            //       indicator: "red",
            //     });
            //   }
            },
            error: function (error) {
              // Handle deadlock errors and retry
              if (error.message && error.message.includes("Deadlock Occurred") && retryCount < MAX_RETRIES) {
                frappe.msgprint({
                  title: __("Deadlock Occurred"),
                  message: __("Retrying..."),
                  indicator: "orange",
                });
                // Retry after delay
                setTimeout(() => {
                  processSingleInvoice(invoiceIndex, retryCount + 1);
                }, DELAY_BETWEEN_REQUESTS);
              } 
            //   else {
            //     frappe.msgprint({
            //       title: __("Payment Processing Failed"),
            //       message: __("Unable to process Invoice {0} and Payment {1}. Maximum retry attempts reached.", [invoiceName, paymentName]),
            //       indicator: "red",
            //     });
            //   }
            },
          });
        }, paymentIndex * DELAY_BETWEEN_REQUESTS);
      });

      // Move to the next invoice after processing all payments for the current invoice
      setTimeout(() => {
        processSingleInvoice(invoiceIndex + 1);
      }, (mpesa_payments.length + 1) * DELAY_BETWEEN_REQUESTS);
    };

    // Start processing the first invoice
    processSingleInvoice(0);
  },

});

function check_for_process_payments_button(frm) {
  if (frm.doc.invoices.length > 0 && frm.doc.mpesa_payments.length > 0) {
    let process_btn = frm.add_custom_button(__("Process Payments"), () => {
      frm.trigger("process_payments");
    });

    process_btn.addClass("btn-primary");
  }
}
