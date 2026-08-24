frappe.query_reports["Delivery Performance And POD Pending"] = {
	filters: [
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.month_start() },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.month_end() },
		{ fieldname: "vehicle", label: __("Vehicle"), fieldtype: "Link", options: "Vehicle" },
		{ fieldname: "pod_pending_only", label: __("POD Pending Only"), fieldtype: "Check", default: 1 },
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "pod_status" && data && data.pod_status !== "Received") {
			value = `<span style="color:var(--text-danger);font-weight:600">${value}</span>`;
		}
		return value;
	},
};
