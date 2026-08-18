// Copyright (c) 2025, Navari Limited and contributors
// For license information, please see license.txt

frappe.query_reports["B2C Disbursement Summary"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "start_date",
			label: __("Start Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -5),
			reqd: 1,
		},
		{
			fieldname: "end_date",
			label: __("End Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "party_type",
			label: __("Party Type"),
			fieldtype: "Link",
			options: "DocType",
			get_query: function () {
				return {
					filters: {
						name: ["in", ["Supplier", "Employee"]],
					},
				};
			},
		},
		{
			fieldname: "party",
			label: __("Party"),
			fieldtype: "Dynamic Link",
			options: "party_type",
			get_query: function () {
				const selected_party_type = frappe.query_report.get_filter_value("party_type");

				if (selected_party_type == "Employee") {
					return {
						filters: {
							status: "Active",
						},
					};
				}
			},
			depends_on: "eval:doc.party_type",
			mandatory_depends_on: "eval:doc.party_type",
		},
		{
			fieldname: "transaction_to_pay_against",
			label: __("Transaction to Pay Against"),
			fieldtype: "Link",
			options: "DocType",
			get_query: function () {
				const doctypes =
					frappe.query_report.get_filter_value("party_type") === "Employee"
						? ["Salary Slip", "Employee Advance", "Expense Claim", "Loan"]
						: ["Purchase Invoice", "Purchase Order"];
				return {
					filters: {
						name: ["in", doctypes],
					},
				};
			},
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		let formatted_value = default_formatter(value, row, column, data);

		if (data && data.is_subtotal) {
			formatted_value = `<span style="font-weight: bold;">${formatted_value}</span>`;
		}

		const blankFields = ["total_amount"];

		const isBlankableColumn =
			blankFields.includes(column.fieldname) || /\d+$/.test(column.fieldname);

		const shouldBlank =
			data &&
			(value === null ||
				value === undefined ||
				value === "" ||
				value === 0 ||
				value === "0%" ||
				formatted_value === "Sh 0.00");

		if (isBlankableColumn && shouldBlank) {
			return "";
		}

		return formatted_value;
	},
};
