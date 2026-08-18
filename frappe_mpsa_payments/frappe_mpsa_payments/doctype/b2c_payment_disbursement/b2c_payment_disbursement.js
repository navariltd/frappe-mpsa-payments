// Copyright (c) 2024, Navari Limited and contributors
// For license information, please see license.txt

const HAS_ERPNEXT = frappe.boot?.app_data?.some((app) => app.app_name === "erpnext");

// Frappe runs doctype scripts through `new Function(...)`, so a bare top-level
// return works at runtime - but the file is linted as a module, where it is a
// syntax error. Hold the handlers and register them behind the guard instead.
const B2C_HANDLERS = {
	onload: function (frm) {
		frm.ignore_doctypes_on_cancel_all = [
			"Employee Advance",
			"Expense Claim",
			"Purchase Invoice",
			"Purchase Order",
			"Salary Slip",
		];

		if (frm.is_new()) {
			frm.set_value("party_type", "Employee");
			frm.set_value("paid_from_account_currency", null);
			frm.set_value("paid_to_account_currency", null);
			frm.set_value("source_exchange_rate", 1);
			frm.set_value("target_exchange_rate", 1);
			frm.set_value(
				"company_currency",
				frappe.get_doc(":Company", frm.doc.company)?.default_currency
			);
		}

		setTimeout(() => {
			const button_field = frm.fields_dict.get_references;
			const $btn = button_field.$wrapper.find(".btn");
			$btn.removeClass("btn-xs").addClass("btn-sm btn-block text-left");
		}, 150);

		// Prevent duplicate realtime listeners
		if (!frm._b2c_listener_bound) {
			frm._b2c_listener_bound = true;

			frappe.realtime.on("b2c_payment_update", (data) => {
				const { docname, row_number, party, allocated_amount, status } = data;

				if (frm && frm.doc.name === docname) {
					const color =
						status === "Paid" ? "green" : status === "Failed" ? "red" : "orange";

					const details = `
              <div>
                <b>Row:</b> ${row_number}
                ${party ? `<b>Party:</b> ${party}` : ""}
                ${
					allocated_amount
						? `<b>Amount:</b> KES ${flt(allocated_amount).toLocaleString()}`
						: ""
				}
                <b>Status:</b> ${status}
              </div>
            `;

					frappe.msgprint({
						title: `MPesa Payment: ${status}`,
						indicator: color,
						message: details,
					});

					frm.reload_doc().then(() => {
						frm.refresh_fields("references");
					});
				}
			});
		}
	},

	setup: function (frm) {
		frm.set_query("paid_from", function () {
			frm.events.validate_company(frm);
			return {
				filters: {
					account_type: ["in", ["Bank", "Cash"]],
					is_group: 0,
					company: frm.doc.company,
				},
			};
		});

		frm.set_query("paid_to", function () {
			frm.events.validate_company(frm);
			var account_types = frm.doc.party_type == "Employee" ? ["Asset"] : ["Payable"];
			// return {
			//   filters: {
			//     account_type: ["in", account_types],
			//     is_group: 0,
			//     company: frm.doc.company,
			//   },
			// };
		});

		frm.set_query("party_type", function () {
			frm.events.validate_company(frm);
			return {
				filters: {
					name: ["in", ["Employee", "Supplier"]],
				},
			};
		});

		frm.set_query("transaction_to_pay_against", function () {
			const doctypes =
				frm.doc.party_type === "Employee"
					? ["Salary Slip", "Employee Advance", "Expense Claim", "Loan"]
					: ["Purchase Invoice", "Purchase Order"];
			return {
				filters: { name: ["in", doctypes] },
			};
		});

		frm.set_query("reference_doctype", "references", function () {
			let doctypes = [];
			if (frm.doc.party_type == "Employee") {
				doctypes = ["Employee Advance", "Expense Claim", "Salary Slip", "Loan"];
			} else if (frm.doc.party_type == "Supplier") {
				doctypes = ["Purchase Invoice", "Purchase Order"];
			}
			return {
				filters: { name: ["in", doctypes] },
			};
		});

		frm.set_query("reference_name", "references", function (doc, cdt, cdn) {
			const child = locals[cdt][cdn];
			const filters = { docstatus: 1, company: doc.company };

			if (
				["Employee Advance", "Expense Claim", "Salary Slip"].includes(
					child.reference_doctype
				)
			) {
				filters.employee = child.party;
				if (child.reference_doctype === "Salary Slip") {
					filters.status = ["!=", "Draft"];
				}
			} else if (["Purchase Invoice", "Purchase Order"].includes(child.reference_doctype)) {
				filters.supplier = child.party;
			}

			return { filters: filters };
		});

		frm.set_query("party", "references", function (doc, cdt, cdn) {
			const child = locals[cdt][cdn];
			if (child.party_type == "Employee") {
				return {
					query: "erpnext.controllers.queries.employee_query",
					filters: { company: doc.company },
				};
			} else if (child.party_type == "Supplier") {
				return {
					filters: { company: doc.company },
				};
			}
		});
	},

	refresh: function (frm) {
		// Check if there are any failed entries
		let has_failed_references = frm.doc.references.some(
			(reference) => reference.payment_status === "Failed"
		);

		if (has_failed_references && frm.doc.docstatus === 1) {
			frm.add_custom_button(
				__("Retry Failed Transactions"),
				function () {
					frappe.confirm("Retry failed disbursements?", function () {
						frappe.call({
							method: "retry_failed_payments",
							doc: frm.doc,
							args: {},
							callback: function (r) {
								if (r.message) {
									frappe.msgprint(r.message);
									frm.reload_doc();
								}
							},
						});
					});
				},
				"Actions"
			);
		}

		if (frm.doc.docstatus === 1 && !["Paid", "Not Initiated"].includes(frm.doc.status)) {
			frm.add_custom_button(
				__("Check Latest Payment Status"),
				() => {
					frappe.call({
						method: "update_disbursement_status",
						doc: frm.doc,
						args: {},
						callback: function (r) {
							if (r.message) {
								frappe.msgprint(r.message);
								frm.reload_doc();
							}
						},
					});
				},
				"Actions"
			);
		}
	},

	validate: function (frm) {
		frm.events.validate_company(frm);
		if (!frm.doc.party_type) {
			frappe.throw({ message: __("Please select Party Type."), title: __("Mandatory") });
		}
		if (!frm.doc.references.length) {
			frappe.throw({
				message: __("At least one reference is required."),
				title: __("Mandatory"),
			});
		}

		let invalid_references = frm.doc.references.filter(
			(row) => row.party_type !== frm.doc.party_type
		);
		if (invalid_references.length) {
			frappe.throw(
				__(
					"All references must have Party Type matching the document: " +
						frm.doc.party_type
				)
			);
		}
	},

	validate_company: function (frm) {
		if (!frm.doc.company) {
			frappe.throw({
				message: __("Please select a company first."),
				title: __("Mandatory"),
			});
		}
	},

	company: function (frm) {
		frm.set_value(
			"company_currency",
			frappe.get_doc(":Company", frm.doc.company)?.default_currency
		);
	},

	mode_of_payment: function (frm) {
		if (frm.doc.mode_of_payment) {
			frappe.call({
				method: "frappe_mpsa_payments.utils.utils.get_mode_of_payment_account",
				args: {
					mode_of_payment: frm.doc.mode_of_payment,
					company: frm.doc.company,
				},
				callback: function (r) {
					if (r.message) {
						frm.set_value("paid_from", r.message);
					} else {
						frm.set_value("paid_from", null);
					}
				},
			});
		} else {
			frm.set_value("paid_from", null);
		}
	},

	party_type: function (frm) {
		frm.set_value("transaction_to_pay_against", null);
		frm.set_value("paid_to", null);
		frm.clear_table("references");
		frm.refresh_field("references");
	},

	transaction_to_pay_against: function (frm) {
		const doctype = frm.doc.transaction_to_pay_against;
		const company = frm.doc.company;

		frm.clear_table("references");
		frm.refresh_field("references");

		if (!doctype) {
			frm.set_value("paid_to", null);
			return;
		}

		const accountFieldMap = {
			"Employee Advance": "default_employee_advance_account",
			"Salary Slip": "default_payroll_payable_account",
			"Expense Claim": "default_expense_claim_payable_account",
			"Purchase Invoice": "default_payable_account",
			"Purchase Order": "default_payable_account",
		};

		const accountField = accountFieldMap[doctype];

		if (accountField) {
			frappe.db.get_value("Company", company, accountField).then((response) => {
				const account = response.message ? response.message[accountField] : null;
				if (account) {
					frm.set_value("paid_to", account);
				}
			});
		} else {
			frappe.db.get_value("Company", company, "default_payable_account").then((response) => {
				const account = response.message ? response.message[accountField] : null;
				if (account) {
					frm.set_value("paid_to", account);
				}
			});
		}
	},

	paid_from: function (frm) {
		frm.events.set_account_currency(
			frm,
			frm.doc.paid_from,
			"paid_from_account_currency",
			function (frm) {
				frm.events.set_current_exchange_rate(
					frm,
					"source_exchange_rate",
					frm.doc.paid_from_account_currency,
					frm.doc.company_currency
				);
			}
		);
	},

	paid_to: function (frm) {
		frm.events.set_account_currency(
			frm,
			frm.doc.paid_to,
			"paid_to_account_currency",
			function (frm) {
				frm.events.set_current_exchange_rate(
					frm,
					"target_exchange_rate",
					frm.doc.paid_to_account_currency,
					frm.doc.company_currency
				);
			}
		);
	},

	set_account_currency: function (frm, account, currency_field, callback_function) {
		if (frm.doc.posting_date && account) {
			frappe.call({
				method: "erpnext.accounts.doctype.payment_entry.payment_entry.get_account_details",
				args: {
					account: account,
					date: frm.doc.posting_date,
				},
				callback: function (r) {
					if (r.message) {
						frappe.run_serially([
							() => frm.set_value(currency_field, r.message["account_currency"]),
							() => {
								if (callback_function) callback_function(frm);
							},
						]);
					}
				},
			});
		}
	},

	source_exchange_rate: function (frm) {
		if (frm.doc.paid_amount) {
			frm.set_value(
				"base_paid_amount",
				flt(frm.doc.paid_amount) * flt(frm.doc.source_exchange_rate)
			);
			frm.events.calculate_paid_amount_kes(frm);
			frm.events.allocate_party_amount_against_ref_docs(frm, frm.doc.paid_amount);
		}
		frm.set_df_property(
			"source_exchange_rate",
			"read_only",
			erpnext.stale_rate_allowed() ? 0 : 1
		);
	},

	target_exchange_rate: function (frm) {
		frm.events.allocate_party_amount_against_ref_docs(frm, frm.doc.paid_amount);
		frm.set_df_property(
			"source_exchange_rate",
			"read_only",
			erpnext.stale_rate_allowed() ? 0 : 1
		);
	},

	paid_amount: function (frm) {
		if (frm.doc.source_exchange_rate) {
			frm.set_value(
				"base_paid_amount",
				flt(frm.doc.paid_amount) * flt(frm.doc.source_exchange_rate)
			);
		}
		frm.events.calculate_paid_amount_kes(frm);
		frm.events.allocate_party_amount_against_ref_docs(frm, frm.doc.paid_amount);
	},

	calculate_paid_amount_kes: function (frm) {
		let company_currency = frm.doc.company_currency;
		if (frm.doc.paid_from_account_currency === "KES") {
			frm.set_value("base_paid_amount", frm.doc.paid_amount);
		} else {
			frappe.call({
				method: "erpnext.setup.utils.get_exchange_rate",
				args: {
					from_currency: company_currency,
					to_currency: "KES",
					transaction_date: frm.doc.posting_date,
				},
				callback: function (r) {
					if (r.message) {
						let kes_exchange_rate = flt(r.message);
						frm.set_value(
							"base_paid_amount",
							flt(frm.doc.base_paid_amount) / kes_exchange_rate
						);
					}
				},
			});
		}
	},

	get_outstanding_references: function (frm, get_references) {
		const today = frappe.datetime.get_today();
		let fields = [
			{ fieldtype: "Section Break", label: __("Party") },
			{
				fieldtype: "Link",
				label: __("Party"),
				fieldname: "party",
				options: frm.doc.party_type,
				depends_on: () => frm.doc.transaction_to_pay_against !== "Salary Slip",
				get_query: () => {
					const party_type = frm.doc.party_type;
					let filters = {};

					if (party_type === "Employee") {
						filters.status = "Active";
					} else if (party_type === "Supplier") {
						filters.disabled = 0;
					}

					return { filters };
				},
			},
			{
				fieldtype: "Link",
				label: __("Payroll Entry"),
				fieldname: "payroll_entry",
				options: "Payroll Entry",
				depends_on: () => frm.doc.transaction_to_pay_against === "Salary Slip",
				get_query: () => {
					return {
						filters: {
							docstatus: 1,
						},
					};
				},
			},
			{ fieldtype: "Column Break" },
			{ fieldtype: "Section Break", label: __("Posting Date") },
			{
				fieldtype: "Date",
				label: __("From Date"),
				fieldname: "from_posting_date",
				default: frappe.datetime.add_days(today, -30),
			},
			{ fieldtype: "Column Break" },
			{
				fieldtype: "Date",
				label: __("To Date"),
				fieldname: "to_posting_date",
				default: today,
			},
			{ fieldtype: "Section Break", label: __("Due Date") },
			{ fieldtype: "Date", label: __("From Date"), fieldname: "from_due_date" },
			{ fieldtype: "Column Break" },
			{ fieldtype: "Date", label: __("To Date"), fieldname: "to_due_date" },
			{ fieldtype: "Section Break", label: __("Outstanding Amount") },
			{
				fieldtype: "Float",
				label: __("Greater Than Amount"),
				fieldname: "outstanding_amt_greater_than",
				default: 0,
			},
			{ fieldtype: "Column Break" },
			{
				fieldtype: "Float",
				label: __("Less Than Amount"),
				fieldname: "outstanding_amt_less_than",
			},
		];

		fields = fields.concat([
			{ fieldtype: "Section Break" },
			{
				fieldtype: "Check",
				label: __("Allocate Payment Amount"),
				fieldname: "allocate_payment_amount",
				default: 1,
			},
		]);

		const btn_text = "Get Outstanding References";

		frappe.prompt(
			fields,
			function (filters) {
				frappe.flags.allocate_payment_amount = true;
				frm.events.validate_filters_data(frm, filters);
				frm.events.get_outstanding_documents(
					frm,
					filters,
					frm.events.get_outstanding_references
				);
			},
			__("Filters"),
			__(btn_text)
		);
	},

	get_references(frm) {
		frm.events.get_outstanding_references(frm, true);
	},

	validate_filters_data: function (frm, filters) {
		const fields = {
			"Posting Date": ["from_posting_date", "to_posting_date"],
			"Due Date": ["from_due_date", "to_due_date"],
			"Outstanding Amount": ["outstanding_amt_greater_than", "outstanding_amt_less_than"],
		};

		for (let label in fields) {
			let [from_field, to_field] = fields[label];
			let from_value = filters[from_field];
			let to_value = filters[to_field];

			if (from_value && !to_value) {
				frappe.throw(
					__(
						`Please enter <b>${to_field.replace(
							/_/g,
							" "
						)}</b> to complete the range for <b>${label}</b>`
					)
				);
			}

			if (from_value && to_value && from_value > to_value) {
				frappe.throw(
					__(
						`For <b>${label}</b>, <b>${from_field.replace(
							/_/g,
							" "
						)}</b> must be less than <b>${to_field.replace(/_/g, " ")}</b>.`
					)
				);
			}
		}

		if (frm.doc.transaction_to_pay_against === "Salary Slip" && !filters.payroll_entry) {
			frappe.throw(__("Payroll Entry is required when paying against Salary Slip."));
		}
	},

	get_outstanding_documents: function (frm, filters) {
		frm.clear_table("references");
		frm.events.check_mandatory_to_fetch(frm);

		let args = {
			posting_date: frm.doc.posting_date,
			company: frm.doc.company,
			payment_type: frm.doc.payment_type,
			party_account: frm.doc.paid_to,
			party_type: frm.doc.party_type,
			transaction_to_pay_against: frm.doc.transaction_to_pay_against,
			party: filters.party,
			payroll_entry: filters.payroll_entry,
			from_posting_date: filters.from_posting_date,
			to_posting_date: filters.to_posting_date,
			from_due_date: filters.from_due_date,
			to_due_date: filters.to_due_date,
			outstanding_amt_greater_than: filters.outstanding_amt_greater_than,
		};

		frappe.flags.allocate_payment_amount = filters["allocate_payment_amount"];

		frappe.call({
			method: "get_outstanding_reference_documents",
			doc: frm.doc,
			args: args,
			callback: function (r) {
				if (r.message) {
					frm.refresh_field("references");
					if (frappe.flags.allocate_payment_amount) {
						frm.events.allocate_party_amount_against_ref_docs(
							frm,
							frm.doc.paid_amount
						);
					} else {
						frappe.msgprint({
							message: __("References populated successfully"),
							title: __("Success"),
							indicator: "green",
						});
					}
				}
			},
		});
	},

	check_mandatory_to_fetch: function (frm) {
		if (!frm.doc.company) {
			frappe.msgprint(__("Please select Company first"));
			frappe.validated = false;
		}
		if (!frm.doc.paid_to) {
			frappe.msgprint(__("Please Select Account Paid To first"));
			frappe.validated = false;
		}
		if (!frm.doc.party_type) {
			frappe.msgprint(__("Please select Party Type first"));
			frappe.validated = false;
		}
	},

	set_current_exchange_rate: function (frm, exchage_rate_field, from_currency, to_currency) {
		if (from_currency === to_currency) {
			frm.set_value(exchage_rate_field, 1);
			return;
		}
		frappe.call({
			method: "erpnext.setup.utils.get_exchange_rate",
			args: {
				transaction_date: frm.doc.posting_date,
				from_currency: from_currency,
				to_currency: to_currency,
			},
			callback: function (r) {
				if (r.message) {
					frm.set_value(exchage_rate_field, flt(r.message));
				}
			},
		});
	},
	allocate_party_amount_against_ref_docs: function (frm, paid_amount) {
		if (!frm.doc.references || !frm.doc.references.length) {
			return;
		}

		frappe.call({
			method: "allocate_amount_to_references",
			doc: frm.doc,
			args: {
				paid_amount: paid_amount || frm.doc.paid_amount,
				paid_amount_change: 1,
				allocate_payment_amount: frappe.flags.allocate_payment_amount || 1,
			},
			callback: function (r) {
				frm.refresh_field("references");
				frappe.msgprint({
					message: __("References populated and allocated successfully"),
					title: __("Success"),
					indicator: "green",
				});
			},
			error: function (r) {
				frappe.msgprint({
					message: __("Error allocating amounts: " + r.message),
					title: __("Error"),
					indicator: "red",
				});
			},
		});
	},
};

if (HAS_ERPNEXT) {
	frappe.ui.form.on("B2C Payment Disbursement", B2C_HANDLERS);
}

function generateUUIDv4() {
	// Generates a uuid4 string conforming to RFC standards
	let uuid = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
		let r = (Math.random() * 16) | 0,
			v = c === "x" ? r : (r & 0x3) | 0x8;
		return v.toString(16);
	});
	return uuid;
}

function validatePhoneNumber(phoneNumber) {
	// Validates the receiver phone numbers
	if (phoneNumber.startsWith("2547")) {
		const pattern = /^2547\d{8}$/;
		return pattern.test(phoneNumber);
	} else {
		const pattern = /^(25410|25411)\d{7}$/;
		return pattern.test(phoneNumber);
	}
}

function sanitisePhoneNumber(phoneNumber) {
	phoneNumber = phoneNumber.replace("+", "").replace(/\s/g, "");

	const regex = /^0\d{9}$/;
	if (!regex.test(phoneNumber)) {
		return phoneNumber;
	}

	phoneNumber = "254" + phoneNumber.substring(1);
	return phoneNumber;
}
