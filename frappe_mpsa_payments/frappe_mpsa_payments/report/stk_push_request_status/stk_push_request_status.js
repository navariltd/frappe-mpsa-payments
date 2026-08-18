// Copyright (c) 2025, Navari Limited and contributors
// For license information, please see license.txt

frappe.query_reports["STK Push Request Status"] = {
	filters: [
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nPending\nSuccess\nFailed",
			default: "Pending",
		},
		{
			fieldname: "start_date",
			label: __("Start Date"),
			fieldtype: "Date",
			default: "2025-01-01",
		},
		{
			fieldname: "end_date",
			label: __("End Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "voucher_type",
			label: __("Voucher Type"),
			fieldtype: "Select",
			options: "Purchase Invoice\nSales Invoice\nPOS Invoice",
			default: "Purchase Invoice",
		},
		{
			fieldname: "voucher_no",
			label: __("Voucher No"),
			fieldtype: "Link",
		},
		{
			fieldname: "phone_number",
			label: __("Phone Number"),
			fieldtype: "Data",
			description: __("Filter by phone number (optional)"),
			default: "",
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status") {
			const colorMap = {
				Success: "green",
				Failed: "red",
				Pending: "orange",
			};
			const color = colorMap[value];
			if (color) {
				value = `<span style="color: ${color}; font-weight: bold;">${value}</span>`;
			}
		}
		return value;
	},
};
