// Copyright (c) 2025, Navari Limited and contributors
// For license information, please see license.txt

frappe.query_reports["C2B Payment Summary Group by Customer"] = {
	filters: [
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
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nDraft\nSubmitted\nCancelled",
		},
		{
			fieldname: "group_by_customer",
			label: __("Group by Customer"),
			fieldtype: "Check",
			default: 0,
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		let formatted_value = default_formatter(value, row, column, data);

		if (data && data.is_subtotal) {
			formatted_value = `<span style="font-weight: bold;">${formatted_value}</span>`;
		}

		const blankFields = ["amount", "transamount"];

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

		if (column.fieldname === "status") {
			const colorMap = {
				Submitted: "green",
				Cancelled: "red",
				Draft: "orange",
			};

			const color = colorMap[value];

			if (color) {
				formatted_value = `<span style="color: ${color}; font-weight: bold;">${formatted_value}</span>`;
			}
		}

		return formatted_value;
	},
};
