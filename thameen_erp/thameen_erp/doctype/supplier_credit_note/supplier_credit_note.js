frappe.ui.form.on("Supplier Credit Note", {
	setup(frm) {
		frm.set_query("purchase_invoice", () => ({
			filters: { supplier: frm.doc.supplier, docstatus: 1, is_return: 0 },
		}));
	},

	refresh(frm) {
		if (frm.doc.debit_note) {
			frm.add_custom_button(__("Debit Note"), () =>
				frappe.set_route("Form", "Purchase Invoice", frm.doc.debit_note), __("View"));
		}
		if (frm.doc.docstatus === 0 && frm.doc.purchase_invoice) {
			frm.add_custom_button(__("Fetch Expected Lines"), () => fetch_lines(frm));
		}
		render_variance(frm);
	},

	get_items(frm) {
		fetch_lines(frm);
	},

	purchase_invoice(frm) {
		if (frm.doc.purchase_invoice && !(frm.doc.items || []).length) fetch_lines(frm);
	},
});

frappe.ui.form.on("Supplier Credit Note Item", {
	actual_amount(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, "variance",
			flt(row.actual_amount) - flt(row.expected_amount));
	},
});

function fetch_lines(frm) {
	frappe.call({
		method: "thameen_erp.thameen_erp.doctype.supplier_credit_note.supplier_credit_note.get_expected_lines",
		args: { purchase_invoice: frm.doc.purchase_invoice },
		freeze: true,
		callback({ message }) {
			if (!message || !message.length) {
				frappe.msgprint(__("No lines on this invoice are awaiting a credit note."));
				return;
			}
			frm.clear_table("items");
			message.forEach((line) => {
				const row = frm.add_child("items");
				Object.assign(row, {
					purchase_invoice_item: line.purchase_invoice_item,
					item_code: line.item_code,
					qty: line.qty,
					expected_amount: line.expected_amount,
					actual_amount: line.actual_amount,
				});
			});
			frm.refresh_field("items");
		},
	});
}

function render_variance(frm) {
	if (!frm.doc.total_expected) return;
	const v = flt(frm.doc.total_variance);
	const colour = Math.abs(v) < 0.01 ? "green" : v < 0 ? "orange" : "red";
	frm.dashboard.add_comment(
		__("Expected {0} · Actual {1} · Variance {2} ({3}%)", [
			format_currency(frm.doc.total_expected),
			format_currency(frm.doc.total_actual),
			format_currency(v),
			flt(frm.doc.variance_percent, 2),
		]),
		colour, true);
}
