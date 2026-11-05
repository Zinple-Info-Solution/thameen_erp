frappe.query_reports["Rebate Report"] = {
	filters: [
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["Supplier Wise", "Credit Note Wise"],
			default: "Supplier Wise",
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "supplier",
			label: __("Supplier"),
			fieldtype: "Link",
			options: "Supplier",
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
		},
		{
			fieldname: "only_outstanding",
			label: __("Only Suppliers With Outstanding Balance"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		if (column.fieldname === "credit_notes" && value) {
			return value
				.split(", ")
				.map(
					(name) =>
						`<a href="/app/supplier-credit-note/${encodeURIComponent(name)}" target="_blank">${name}</a>`
				)
				.join(", ");
		}
		return default_formatter(value, row, column, data);
	},
};
