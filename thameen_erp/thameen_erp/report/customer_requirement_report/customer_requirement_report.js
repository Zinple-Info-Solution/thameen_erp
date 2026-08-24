frappe.query_reports["Customer Requirement Report"] = {
	filters: [
		{ fieldname: "customer", label: __("Customer"), fieldtype: "Link", options: "Customer" },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.add_months(frappe.datetime.get_today(), -3) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today() },
		{ fieldname: "status", label: __("Status"), fieldtype: "Select",
		  options: "\nDraft\nPending Sales Approval\nPending Finance Approval\nApproved\nRejected\nOrdered\nCancelled" },
		{ fieldname: "pending_only", label: __("Pending Approval Only"), fieldtype: "Check" },
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "credit_flag" && data && !data.credit_check_passed) {
			value = `<span style="color:var(--text-danger);font-weight:600">${value}</span>`;
		}
		return value;
	},
};
