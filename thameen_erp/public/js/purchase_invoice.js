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

	custom_add_rebate(frm) {
		recalculate_all_rebates(frm);
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

	qty(frm, cdt, cdn) {
		calculate_row_rebate(frm, cdt, cdn);
	},
	price_list_rate(frm, cdt, cdn) {
		calculate_row_rebate(frm, cdt, cdn);
	},
	item_code(frm, cdt, cdn) {
		setTimeout(() => calculate_row_rebate(frm, cdt, cdn), 300);
	},
	custom_rebate_percentage(frm, cdt, cdn) {
		calculate_row_rebate(frm, cdt, cdn);
	},
});

// ─── Supplier rebate ──────────────────────────────────────────────

function calculate_row_rebate(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row) return;

	const base_rate = flt(row.price_list_rate) || flt(row.rate);
	const percentage = frm.doc.custom_add_rebate ? flt(row.custom_rebate_percentage) : 0;
	const rebate_per_unit = (base_rate * percentage) / 100;
	const new_rate = base_rate - rebate_per_unit;

	frappe.model.set_value(cdt, cdn, "custom_rebate_amount", rebate_per_unit * flt(row.qty));

	if (flt(row.rate).toFixed(4) !== new_rate.toFixed(4)) {
		// triggers ERPNext's built-in amount / net_total / grand_total recalculation
		frappe.model.set_value(cdt, cdn, "rate", new_rate);
	}
}

function recalculate_all_rebates(frm) {
	(frm.doc.items || []).forEach((row) => calculate_row_rebate(frm, row.doctype, row.name));
}
