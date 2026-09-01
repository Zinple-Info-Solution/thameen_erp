frappe.listview_settings["Delivery Trip"] = {
	// The row title is the trip ID (property setter clears the driver-name
	// title). Columns: status, vehicle, route, trip start, trip end.
	add_fields: [
		"status", "vehicle", "driver", "custom_pod_received",
		"custom_trip_source", "custom_trip_route", "custom_destination_type",
		"custom_supply_source", "custom_sales_order", "custom_purchase_order",
		"departure_time", "custom_trip_start", "custom_trip_end",
		"custom_trip_duration_hours",
	],
	get_indicator(doc) {
		const map = {
			Draft: "grey", Scheduled: "blue", Loading: "orange", "In Transit": "purple",
			Delivered: "yellow", "POD Pending": "red", Completed: "green", Cancelled: "grey",
		};
		return [__(doc.status), map[doc.status] || "grey", "status,=," + doc.status];
	},
	formatters: {
		custom_trip_route(value, df, doc) {
			if (!value) return "";
			const ref = doc.custom_sales_order || doc.custom_purchase_order || "";
			const label =
				doc.custom_destination_type === "Decide After Loading"
					? `<span class="text-warning">${__(value)}</span>`
					: __(value);
			return `${label}${ref ? " · " + frappe.utils.escape_html(ref) : ""}`;
		},
		// A trip that has started but not finished shows how long it has been
		// out, which is the number dispatch actually watches.
		custom_trip_start(value) {
			if (!value) return `<span class="text-muted">${__("not started")}</span>`;
			return frappe.datetime.str_to_user(value);
		},
		custom_trip_end(value, df, doc) {
			if (value) return frappe.datetime.str_to_user(value);
			if (!doc.custom_trip_start) return "";
			const hours = frappe.datetime.get_hour_diff(
				frappe.datetime.now_datetime(),
				doc.custom_trip_start
			);
			return `<span class="text-muted">${__("out {0}h", [Math.max(Math.round(hours), 0)])}</span>`;
		},
	},
};
