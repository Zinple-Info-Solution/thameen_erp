frappe.ui.form.on("Vehicle Document", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (["Expiring Soon", "Expired"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Renew"), () => {
				frappe.prompt(
					{ fieldname: "new_expiry_date", fieldtype: "Date",
					  label: __("New Expiry Date"), reqd: 1 },
					({ new_expiry_date }) => {
						frappe.call({
							method: "thameen_erp.thameen_erp.doctype.vehicle_document.vehicle_document.renew",
							args: { source_name: frm.doc.name, new_expiry_date },
							callback({ message }) {
								if (message) frappe.set_route("Form", "Vehicle Document", message);
							},
						});
					}, __("Renew Document"), __("Create"));
			}).addClass("btn-primary");
		}
		const colour = { Expired: "red", "Expiring Soon": "orange", Active: "green" }[frm.doc.status];
		if (colour) {
			frm.dashboard.add_comment(
				__("{0} — expires {1}", [frm.doc.status,
					frappe.datetime.str_to_user(frm.doc.expiry_date)]), colour, true);
		}
	},
});
