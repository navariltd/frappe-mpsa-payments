/* global payment_response */

frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (!frm.is_new() && frm.doc.is_pos && frm.doc.docstatus === 0) {
			if (frm.doc.outstanding_amount > 0) {
				frappe.call({
					method: "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.validate_stk_push_eligibility",
					args: {
						docname: frm.doc.name,
						doctype: "Sales Invoice",
					},
					callback: function (response) {
						if (response.message && response.message.eligible) {
							frm.add_custom_button(
								__("Initiate STK Push"),
								function () {
									open_stk_push_dialog(frm, response.message.amount);
								},
								__("Mpesa Actions")
							);
						}
					},
				});
			}
		}
		setup_stk_push_button_logic(frm);
	},

	onload_post_render: function (frm) {
		setup_stk_push_button_logic(frm);
	},

	payment: function (frm) {
		setup_stk_push_button_logic(frm);
	},

	mpesa_payments: function (frm) {
		frm.trigger("open_mpesa_payment_modal");
	},

	open_mpesa_payment_modal: function (frm) {
		// Fetch Mpesa payments
		frappe.call({
			method: "frappe.client.get_list",
			args: {
				doctype: "Mpesa C2B Payment Register",
				filters: {
					docstatus: 0,
				},
				fields: [
					"name",
					"transamount",
					"transid",
					"billrefnumber",
					"mode_of_payment",
					"full_name",
					"posting_date",
				],
			},
			callback: function (response) {
				const payments = response.message;
				if (payments.length) {
					// Create the modal
					let dialog = new frappe.ui.Dialog({
						title: __("Select Mpesa Payment"),
						fields: [
							{
								fieldname: "mpesa_payment",
								label: "Mpesa Payment",
								fieldtype: "Table",
								cannot_add_rows: true,
								data: payments,
								fields: [
									{
										fieldname: "name",
										label: __("Name"),
										fieldtype: "Link",
										options: "Mpesa C2B Payment Register",
										in_list_view: 0,
										read_only: 1,
									},
									{
										fieldname: "posting_date",
										label: __("Posting Date"),
										fieldtype: "Date",
										in_list_view: 1,
										read_only: 1,
									},
									{
										fieldname: "mode_of_payment",
										label: __("Mode of Payment"),
										fieldtype: "Link",
										options: "Mode of Payment",
										in_list_view: 0,
										read_only: 1,
									},
									{
										fieldname: "full_name",
										label: __("Full Name"),
										fieldtype: "Data",
										in_list_view: 1,
										read_only: 1,
									},
									{
										fieldname: "transid",
										label: __("Transaction ID"),
										fieldtype: "Data",
										in_list_view: 1,
										read_only: 1,
									},
									{
										fieldname: "transamount",
										label: __("Payment Amount"),
										fieldtype: "Currency",
										in_list_view: 1,
										read_only: 1,
									},
								],
							},
						],
						primary_action_label: "Add Payment",
						primary_action: function (data) {
							if (data.mpesa_payment && data.mpesa_payment.length > 0) {
								const customer = frm.doc.customer;
								// Loop through selected payments and add them to the Sales Invoice
								data.mpesa_payment.forEach((payment) => {
									// Update the payment with the customer
									frappe.call({
										method: "frappe.client.get",
										args: {
											doctype: "Mpesa C2B Payment Register",
											name: payment.name,
										},
										callback: function (paymentDocResponse) {
											let paymentDoc = paymentDocResponse.message;

											paymentDoc.customer = customer;

											frm.clear_table("payments");

											frm.add_child("payments", {
												mode_of_payment: payment.mode_of_payment,
												amount: payment.transamount,
												reference_no: payment.transid,
											});
											frm.refresh_field("payments");
											dialog.hide();
										},
									});
								});
							} else {
								frappe.msgprint(__("Please select a payment."));
							}
						},
					});
					dialog.show();
				} else {
					frappe.msgprint(__("No draft Mpesa payments available."));
				}
			},
		});
	},

	insert_payment_entry: function (frm) {
		frappe.call({
			method: "frappe.client.insert",
			args: {
				doc: {
					doctype: "Payment Entry",
					payment_type: "Receive",
					party_type: "Customer",
					party: frm.doc.customer,
					paid_to: "Cash",
					reference_no: payment_response["MpesaReceiptNumber"],
					amount: frm.doc.grand_total,
				},
			},
		});
	},
});

frappe.ui.form.on("Sales Invoice Payment", {
	type: function (frm, cdt, cdn) {
		setup_stk_push_button_logic(frm);
	},
	reference_no: function (frm, cdt, cdn) {
		setup_stk_push_button_logic(frm);
	},
	payment_type: function (frm, cdt, cdn) {
		setup_stk_push_button_logic(frm);
	},
	payment_gateway: function (frm, cdt, cdn) {
		setup_stk_push_button_logic(frm);
	},
	mode_of_payment: function (frm, cdt, cdn) {
		setup_stk_push_button_logic(frm);
	},
	phone_number: function (frm, cdt, cdn) {
		setup_stk_push_button_logic(frm);
	},
	amount: function (frm, cdt, cdn) {
		setup_stk_push_button_logic(frm);
	},
});

const STK_PUSH_BUTTON_HTML =
	'<button class="btn btn-primary btn-xs stk-button">Initiate STK Push</button>';

function is_stk_push_row(row) {
	const is_mpesa_or_phone =
		(row.mode_of_payment && row.mode_of_payment.toLowerCase().includes("mpesa")) ||
		row.type === "Phone";

	return Boolean(is_mpesa_or_phone && row.phone_number && row.amount > 0 && !row.reference_no);
}

function setup_stk_push_button_logic(frm) {
	if (frm.is_new()) return;

	const grid = frm.fields_dict.payments?.grid;
	if (!grid || !grid.docfields.some((df) => df.fieldname === "initiate_stk_push")) return;

	// renders the button inside an expanded grid row, where the static cell is hidden
	grid.update_docfield_property("initiate_stk_push", "options", STK_PUSH_BUTTON_HTML);

	const was_dirty = frm.doc.__unsaved;

	(frm.doc.payments || []).forEach((row) => {
		row.initiate_stk_push = is_stk_push_row(row) ? STK_PUSH_BUTTON_HTML : "";
		grid.grid_rows_by_docname[row.name]?.refresh_field("initiate_stk_push");
	});

	frm.doc.__unsaved = was_dirty;

	bind_stk_push_click(frm, grid);
}

function bind_stk_push_click(frm, grid) {
	const wrapper = grid.wrapper[0];
	if (wrapper.dataset.stkPushBound) return;
	wrapper.dataset.stkPushBound = "1";

	// delegated, so the handler survives the grid re-rendering its cells; captured, so the
	// cell listener that reopens the row for editing never sees the click
	wrapper.addEventListener(
		"click",
		(event) => {
			const button = event.target.closest(".stk-button");
			if (!button) return;

			event.preventDefault();
			event.stopPropagation();

			const grid_row = grid.grid_rows.find((row) => row.wrapper?.[0].contains(button));
			if (grid_row) initiate_stk_push_child(frm, grid_row.doc);
		},
		true
	);
}

function initiate_stk_push_child(frm, row) {
	if (row.type !== "Phone") {
		frappe.msgprint(__("STK Push is only allowed for Phone type payments."));
		return;
	}

	if (!row.phone_number) {
		frappe.msgprint(__("Please enter a phone number to initiate STK Push."));
		return;
	}

	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the invoice before initiating STK Push for this payment row."));
		return;
	}

	frappe.call({
		method: "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.get_stk_amount",
		args: {
			payment_name: row.name,
			company: frm.doc.company,
		},
		freeze: true,
		freeze_message: __("Calculating amount for STK Push..."),
		callback(amount_res) {
			const final_amount = amount_res.message;

			if (!final_amount) {
				frappe.msgprint(
					__(
						"STK Push is only supported for KES payments or if company's default currency is KES."
					)
				);
				return;
			}

			frappe.call({
				method: "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.initiate_row_stk_push",
				args: {
					name: row.name,
					phone_number: row.phone_number,
					amount: final_amount,
					mode_of_payment: row.mode_of_payment,
					company: frm.doc.company,
				},
				freeze: true,
				freeze_message: __("Initiating STK Push..."),
				callback(r) {
					if (!r.exc) {
						frappe.msgprint(__("STK Push initiated successfully."));
						frm.refresh();

						setup_stk_push_button_logic(frm);
					}
				},
			});
		},
	});
}

function open_stk_push_dialog(frm, amount) {
	const dialog = new frappe.ui.Dialog({
		title: __("Initiate STK Push"),
		fields: [
			{
				fieldname: "phone_number",
				label: "Phone Number",
				fieldtype: "Data",
				reqd: 1,
				options: "Phone",
			},
			{
				fieldname: "amount",
				label: "Amount (KES)",
				fieldtype: "Currency",
				default: amount,
				reqd: 1,
			},
			{
				fieldname: "mode_of_payment",
				label: "Mode of Payment",
				fieldtype: "Link",
				options: "Mode of Payment",
				get_query: function () {
					return {
						filters: {
							type: "Phone",
						},
					};
				},
				reqd: 1,
			},
		],
		primary_action_label: "Send STK Push",
		primary_action(values) {
			frappe.call({
				method: "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.initiate_invoice_stk_push",
				args: {
					invoice: frm.doc.name,
					phone_number: values.phone_number,
					amount: values.amount,
					mode_of_payment: values.mode_of_payment,
					company: frm.doc.company,
					type: "Sales Invoice",
				},
				callback(r) {
					if (!r.exc) {
						frappe.msgprint(__("STK Push initiated successfully."));
						dialog.hide();
						frm.refresh();
					}
				},
			});
		},
	});

	dialog.show();
}
