// Shared handler for Journal Entry / Expense Claim vehicle cost allocation.
["Journal Entry", "Expense Claim"].forEach((doctype) => {
	frappe.ui.form.on(doctype, {
		custom_vehicle(frm) {
			if (!frm.doc.custom_vehicle) return;
			frappe.db.get_value("Vehicle", frm.doc.custom_vehicle, "custom_cost_center")
				.then(({ message }) => {
					const cc = message && message.custom_cost_center;
					if (!cc) {
						frappe.msgprint(__("Vehicle {0} has no cost center yet. Re-save the Vehicle.",
							[frm.doc.custom_vehicle]));
						return;
					}
					const table = doctype === "Journal Entry" ? "accounts" : "expenses";
					(frm.doc[table] || []).forEach((row) => {
						frappe.model.set_value(row.doctype, row.name, "cost_center", cc);
						frappe.model.set_value(row.doctype, row.name, "custom_vehicle", frm.doc.custom_vehicle);
					});
					frm.refresh_field(table);
				});
		},
	});
});
