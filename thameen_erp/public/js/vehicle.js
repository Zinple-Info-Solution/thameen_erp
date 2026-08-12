frappe.ui.form.on("Vehicle", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.custom_cost_center) {
			frm.add_custom_button(__("Cost Center"), () =>
				frappe.set_route("Form", "Cost Center", frm.doc.custom_cost_center), __("View"));
		}
		if (frm.doc.custom_vehicle_warehouse) {
			frm.add_custom_button(__("Truck Stock"), () =>
				frappe.set_route("query-report", "Stock Balance",
					{ warehouse: frm.doc.custom_vehicle_warehouse }), __("View"));
		}
		frm.add_custom_button(__("Trips"), () =>
			frappe.set_route("List", "Delivery Trip", { vehicle: frm.doc.name }), __("View"));
		frm.add_custom_button(__("Documents"), () =>
			frappe.set_route("List", "Vehicle Document", { vehicle: frm.doc.name }), __("View"));
		frm.add_custom_button(__("Profitability"), () =>
			frappe.set_route("query-report", "Vehicle Profitability",
				{ vehicle: frm.doc.name }), __("View"));

		frm.add_custom_button(__("Add Document"), () => {
			frappe.new_doc("Vehicle Document", { vehicle: frm.doc.name });
		}, __("Create"));
		frm.add_custom_button(__("Log Fuel / Service"), () => {
			frappe.new_doc("Vehicle Log", { license_plate: frm.doc.name });
		}, __("Create"));

		render_status_banner(frm);
	},

	custom_assigned_driver(frm) {
		if (!frm.doc.custom_assigned_driver) return;
		frappe.db.get_value("Driver", frm.doc.custom_assigned_driver, "employee")
			.then(({ message }) => {
				if (message && message.employee) frm.set_value("employee", message.employee);
			});
	},
});

function render_status_banner(frm) {
	frappe.db.get_list("Vehicle Document", {
		filters: { vehicle: frm.doc.name, status: ["in", ["Expired", "Expiring Soon"]] },
		fields: ["name", "document_type", "expiry_date", "status"],
		limit: 10,
	}).then((rows) => {
		if (!rows.length) return;
		const lines = rows.map((r) =>
			`<li>${frappe.utils.escape_html(r.document_type)} — ${r.status} (${frappe.datetime.str_to_user(r.expiry_date)})</li>`
		).join("");
		frm.dashboard.add_comment(
			`<b>${__("Document attention required")}</b><ul>${lines}</ul>`,
			rows.some((r) => r.status === "Expired") ? "red" : "orange",
			true
		);
	});
}
