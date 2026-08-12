frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;
		const pending = (frm.doc.items || []).some(
			(r) => flt(r.custom_expected_discount_amount) > 0 &&
				["Expected", "Partially Received"].includes(r.custom_credit_note_status)
		);
		if (pending) {
			frm.add_custom_button(__("Supplier Credit Note"), () => {
				frappe.new_doc("Supplier Credit Note", {
					supplier: frm.doc.supplier,
					company: frm.doc.company,
					purchase_invoice: frm.doc.name,
				});
			}, __("Create"));
			frm.dashboard.add_comment(
				__("This invoice has credit note lines still outstanding."), "orange", true);
		}
	},
});

frappe.ui.form.on("Purchase Invoice Item", {
	custom_expected_discount_amount(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const qty = flt(row.qty) || 1;
		frappe.model.set_value(cdt, cdn, "custom_agreed_net_price",
			flt(row.rate) - flt(row.custom_expected_discount_amount) / qty);
	},
	rate(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const qty = flt(row.qty) || 1;
		frappe.model.set_value(cdt, cdn, "custom_agreed_net_price",
			flt(row.rate) - flt(row.custom_expected_discount_amount) / qty);
	},
});
