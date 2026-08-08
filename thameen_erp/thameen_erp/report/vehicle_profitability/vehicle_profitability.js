frappe.query_reports["Vehicle Profitability"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
		  default: frappe.defaults.get_user_default("Company"), reqd: 1 },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.add_months(frappe.datetime.get_today(), -1), reqd: 1 },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today(), reqd: 1 },
		{ fieldname: "vehicle", label: __("Vehicle"), fieldtype: "Link", options: "Vehicle" },
		{ fieldname: "vehicle_type", label: __("Vehicle Type"), fieldtype: "Select",
		  options: "\nBulk Cement Tanker\nFlatbed Trailer\nTipper\nTrailer Head\nPickup\nForklift\nOther" },
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "profit" && data) {
			const colour = data.profit < 0 ? "var(--text-danger)" : "var(--text-success)";
			value = `<span style="color:${colour};font-weight:600">${value}</span>`;
		}
		return value;
	},
};
