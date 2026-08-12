frappe.query_reports["Supplier Credit Note Variance"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
		  default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "supplier", label: __("Supplier"), fieldtype: "Link", options: "Supplier" },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.add_months(frappe.datetime.get_today(), -6) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today() },
		{ fieldname: "status", label: __("Status"), fieldtype: "Select",
		  options: "\nExpected\nPartially Received\nFully Received\nReceived Above Expected\nCancelled" },
		{ fieldname: "only_variance", label: __("Only Rows With Variance"), fieldtype: "Check" },
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "age" && data && data.age > 45 && data.pending > 0) {
			value = `<span style="color:var(--text-danger);font-weight:600">${value}</span>`;
		}
		return value;
	},
};
