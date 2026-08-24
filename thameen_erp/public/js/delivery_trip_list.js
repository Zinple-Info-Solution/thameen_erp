frappe.listview_settings["Delivery Trip"] = {
	// The row title is the trip ID (property setter clears the driver-name
	// title). Columns: status, vehicle, driver, source, destination, departure.
	add_fields: [
		"status", "vehicle", "driver", "custom_pod_received",
		"custom_trip_source", "custom_destination_type", "custom_supply_source",
		"custom_sales_order", "custom_purchase_order", "departure_time",
	],
	get_indicator(doc) {
		const map = {
			Draft: "grey", Scheduled: "blue", Loading: "orange", "In Transit": "purple",
			Delivered: "yellow", "POD Pending": "red", Completed: "green", Cancelled: "grey",
		};
		return [__(doc.status), map[doc.status] || "grey", "status,=," + doc.status];
	},
	formatters: {
		custom_trip_source(value, df, doc) {
			if (!value) return "";
			const ref = doc.custom_sales_order || doc.custom_purchase_order || "";
			return `${__(value)}${ref ? " · " + frappe.utils.escape_html(ref) : ""}`;
		},
		custom_destination_type(value) {
			if (value === "Decide After Loading") return `<span class="text-warning">${__(value)}</span>`;
			return value ? __(value) : "";
		},
	},
};
