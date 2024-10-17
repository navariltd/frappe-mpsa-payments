// Copyright (c) 2024, Navari Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on('Mpesa Payments', {
    refresh: function(frm) {
        // Hide the default save button
        frm.disable_save();

        frm.add_custom_button(__('Fetch Entries'), function() {
            frappe.call({
                method: 'frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry.get_outstanding_invoices',
                args: {
                    company: frm.doc.company,
                    currency: frm.doc.currency,
                    customer: "",
                },
                callback: function(response) {
                    let draft_invoices = response.message;
                    if (draft_invoices && draft_invoices.length > 0) {

                        frm.clear_table('invoices');

                        draft_invoices.forEach(function(invoice) {
                            let row = frm.add_child('invoices');
                            row.invoice = invoice.name;
                            row.customer = invoice.customer;
                            row.date = invoice.posting_date;
                            row.total = invoice.grand_total;
                            row.outstanding_amount = invoice.outstanding_amount;
                        });

                        frm.refresh_field('invoices');

                    } else {
                        frappe.msgprint({
                            title: __('No Outstanding Invoices'),
                            message: __('No outstanding invoices were found for the selected customer.'),
                            indicator: 'orange'
                        });
                    }
                }
            });

            frappe.call({
                method: 'frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.get_mpesa_draft_c2b_payments',
                args: {
                    company: frm.doc.company,
                    full_name: frm.doc.full_name ? frm.doc.full_name : "",
                },
                callback: function(response) {
                    let draft_payments = response.message;

                    if (draft_payments && draft_payments.length > 0) {

                        frm.clear_table('mpesa_payments');

                        draft_payments.forEach(function(payment) {
                            let row = frm.add_child('mpesa_payments');
                            row.payment_id = payment.name;
                            row.full_name = payment.full_name;
                            row.date = payment.posting_date;
                            row.amount = payment.transamount;
                        });

                        frm.refresh_field('mpesa_payments');

                    } else {
                        frappe.msgprint({
                            title: __('No Outstanding Payments'),
                            message: __('No outstanding payments were found for the selected customer.'),
                            indicator: 'orange'
                        });
                    }
                }
            });
        });

        frm.get_field("invoices").grid.cannot_add_rows = true;
        refresh_field("invoices");
        frm.get_field("mpesa_payments").grid.cannot_add_rows = true;
        refresh_field("mpesa_payments");
    },
});
