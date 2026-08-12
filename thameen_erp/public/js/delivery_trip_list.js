frappe.listview_settings["Delivery Trip"] = {
	add_fields: ["status", "vehicle", "driver", "custom_pod_received"],
	get_indicator(doc) {
		const map = {
			Draft: "grey", Scheduled: "blue", Loading: "orange", "In Transit": "purple",
			Delivered: "yellow", "POD Pending": "red", Completed: "green", Cancelled: "grey",
		};
		return [__(doc.status), map[doc.status] || "grey", "status,=," + doc.status];
	},
};
