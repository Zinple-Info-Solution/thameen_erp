frappe.ui.form.on("Driver", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Trips"), () =>
			frappe.set_route("List", "Delivery Trip", { driver: frm.doc.name }), __("View"));
		if (frm.doc.expiry_date && frappe.datetime.get_diff(frm.doc.expiry_date,
			frappe.datetime.get_today()) < 30) {
			frm.dashboard.add_comment(
				__("Driving licence expires on {0}.",
					[frappe.datetime.str_to_user(frm.doc.expiry_date)]), "red", true);
		}
	},
});
