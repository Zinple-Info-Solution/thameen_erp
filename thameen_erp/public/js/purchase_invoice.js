frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		if (frm.doc.is_return) {
			// Debit notes carry their own title/QR/original-invoice print
			// format -- the standard Purchase Invoice layout has none of that.
			frm.meta.default_print_format = "Debit Note";
		}
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
		// Turning rebate tracking off clears whatever amount was typed in --
		// turning it on leaves every row blank for the real amount to be
		// typed in directly (see overrides/purchase_invoice.py). A PO-linked
		// row carries its own value across unchanged either way, same as
		// Expected Net Price -- untouched here.
		if (!frm.doc.custom_add_rebate) {
			(frm.doc.items || []).forEach((row) => {
				if (row.po_detail || row.pr_detail) return;
				if (flt(row.custom_rebate_amount)) {
					frappe.model.set_value(row.doctype, row.name, "custom_rebate_amount", 0);
				}
			});
		}
	},
});

frappe.ui.form.on("Purchase Invoice Item", {
	// Expected Net Price is the entered value; Rebate Due is derived from
	// it against this row's own rate — recomputed here so it shows live
	// without waiting for a server round-trip (the real figure is set
	// server-side anyway, in thameen_erp.overrides.purchase.
	// _compute_rebate_from_agreed_price, on every save).
	custom_agreed_net_price(frm, cdt, cdn) {
		recalc_rebate_due(cdt, cdn);
	},
	rate(frm, cdt, cdn) {
		recalc_rebate_due(cdt, cdn);
	},
	qty(frm, cdt, cdn) {
		recalc_rebate_due(cdt, cdn);
	},
});

function recalc_rebate_due(cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row) return;
	const agreed = flt(row.custom_agreed_net_price);
	const rate = flt(row.rate);
	const per_unit = agreed && agreed < rate ? rate - agreed : 0;
	frappe.model.set_value(cdt, cdn, "custom_rebate_per_unit", per_unit);
	frappe.model.set_value(cdt, cdn, "custom_expected_discount_amount", per_unit * flt(row.qty));
}
