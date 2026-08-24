frappe.query_reports["Trip Cost Report"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
		  default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.month_start(), reqd: 1 },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.month_end(), reqd: 1 },
		{ fieldname: "vehicle", label: __("Vehicle"), fieldtype: "Link", options: "Vehicle" },
		{ fieldname: "driver", label: __("Driver"), fieldtype: "Link", options: "Driver" },
		{ fieldname: "status", label: __("Status"), fieldtype: "Select",
		  options: "\nScheduled\nLoading\nIn Transit\nDelivered\nPOD Pending\nCompleted\nCancelled" },
	],
};
