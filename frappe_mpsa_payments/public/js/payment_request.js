frappe.ui.form.on("Payment Request", {
	refresh: function (frm) {
		sync_payment_fields(frm);
	},

	mode_of_payment: function (frm) {
		if (frm.doc.mode_of_payment && frm.doc.company) {
			frappe.call({
				method: "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.get_payment_gateway_from_mop",
				args: {
					mode_of_payment: frm.doc.mode_of_payment,
					company: frm.doc.company,
				},
				callback: function (r) {
					if (!r.exc && r.message) {
						frm.set_value("payment_gateway", r.message);
					}
				},
			});
		}
	},

	payment_gateway: function (frm) {
		if (frm.doc.payment_gateway && frm.doc.company) {
			frappe.call({
				method: "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.get_mop_from_payment_gateway",
				args: {
					payment_gateway: frm.doc.payment_gateway,
					company: frm.doc.company,
				},
				callback: function (r) {
					if (!r.exc && r.message) {
						frm.set_value("mode_of_payment", r.message);
					}
				},
			});
		}
	},
});

function sync_payment_fields(frm) {
	if (frm.doc.mode_of_payment && !frm.doc.payment_gateway && frm.doc.company) {
		frappe.call({
			method: "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.get_payment_gateway_from_mop",
			args: {
				mode_of_payment: frm.doc.mode_of_payment,
				company: frm.doc.company,
			},
			callback: function (r) {
				if (!r.exc && r.message) {
					frm.set_value("payment_gateway", r.message);
				}
			},
		});
	} else if (frm.doc.payment_gateway && !frm.doc.mode_of_payment && frm.doc.company) {
		frappe.call({
			method: "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.get_mop_from_payment_gateway",
			args: {
				payment_gateway: frm.doc.payment_gateway,
				company: frm.doc.company,
			},
			callback: function (r) {
				if (!r.exc && r.message) {
					frm.set_value("mode_of_payment", r.message);
				}
			},
		});
	}
}
